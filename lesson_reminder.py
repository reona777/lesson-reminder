import argparse
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    from simple_salesforce import Salesforce
except ImportError:
    print("pip install simple-salesforce")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("pip install requests")
    sys.exit(1)

try:
    from rapidfuzz import fuzz, process
except ImportError:
    print("pip install rapidfuzz")
    sys.exit(1)

JST = timezone(timedelta(hours=9))
WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        print(f"❌ 環境変数 {name} が未設定です")
        sys.exit(1)
    return value


SF_USERNAME = required_env("SF_USERNAME")
SF_PASSWORD = required_env("SF_PASSWORD")
SF_SECURITY_TOKEN = required_env("SF_SECURITY_TOKEN")
GAS_URL = required_env("GAS_URL")
# GAS側の名簿取得・送信は合言葉（スクリプトプロパティ ROSTER_TOKEN）で保護されている。
# 一致しないと doGet も doPost も {"error": "unauthorized"} しか返さない。
ROSTER_TOKEN = required_env("ROSTER_TOKEN")
SLACK_WEBHOOK = required_env("SLACK_WEBHOOK")
SLACK_TEACHER_WEBHOOK = required_env("SLACK_TEACHER_WEBHOOK")


def normalize(s):
    return unicodedata.normalize("NFKC", s).replace(" ", "").replace("　", "").strip()


# 保護者にもLINEを送る対象生徒。名簿シートのH列（保護者通知）のチェックで決まる。
# 実行時に lesson_reminder_runner.py がシートを読んでこの集合を入れ替えるため、
# ここは空で始める。希望者が増えてもコードは触らず、シートのチェックだけで足りる。
# なお本ファイルを単体で実行すると保護者送信は行われない（運用は runner 経由）。
PARENT_NOTIFY_TARGET_NAMES: set = set()


def _load_course_rules() -> list:
    """環境変数 COURSE_RULES からコース名の判定表を読み込む。

    書式は `正規表現:前倒し時間` のカンマ区切り。前から順に評価し、最初に
    一致したものを採用する。前倒し時間は、コースによって「授業の開始時刻が
    レコード上の時刻より N 時間早い」という運用差を吸収するためのもの。

        COURSE_RULES=標準コースL:2,標準コースS:1,短期コース:0

    コース名は組織ごとに違うのでコードに書かない。未設定なら前倒しはせず、
    表示ラベルにはレコードの名称をそのまま使う。
    """
    rules = []
    for entry in os.getenv("COURSE_RULES", "").split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        pattern, _, hours = entry.rpartition(":")
        pattern = pattern.strip()
        try:
            offset = int(hours.strip())
        except ValueError:
            print(f"⚠️ COURSE_RULES の前倒し時間が数値ではありません: {entry}")
            continue
        try:
            rules.append((re.compile(pattern), offset))
        except re.error as e:
            print(f"⚠️ COURSE_RULES の正規表現が不正です: {entry} ({e})")
    return rules


COURSE_RULES: list = _load_course_rules()

# 体験回はコース名が同じでも開始時刻が前倒しにならない。
# 2回目以降の体験（体験2 / 体験２ / 体験②）は通常回と同じ扱いにする。
TRIAL_PATTERN = re.compile(os.getenv("TRIAL_PATTERN", "体験"))
TRIAL_EXCEPT_PATTERN = re.compile(os.getenv("TRIAL_EXCEPT_PATTERN", "体験[2２②]"))

# 名称にこのキーワードを含む授業はリマインドしない（担当未定の仮枠・社内研修など）
SKIP_LESSON_KEYWORDS = [
    kw.strip() for kw in os.getenv("SKIP_LESSON_KEYWORDS", "").split(",") if kw.strip()
]


def utc_to_jst(s):
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(JST)
        return f"{dt.hour}:{dt.minute:02d}"
    except Exception:
        return s


def utc_to_jst_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(JST)
    except Exception:
        return None


def extract_subject(n):
    m = re.search(r"[（(]([^）)]+)[）)]", n)
    if m:
        return m.group(1)
    m = re.search(r":([^:]+)$", n)
    if m and "枠" not in m.group(1):
        return m.group(1).strip()
    return ""


def clean(s):
    return re.sub(r"^\[[^\]]*\]", "", s or "").strip()


def is_skipped_lesson(lesson_name):
    """リマインド対象外にする授業かどうか。

    担当がまだ決まっていない仮押さえの枠や、生徒が関与しない社内向けの枠が
    同じオブジェクトに入っているため、名称のキーワードで除外する。
    どのキーワードで弾くかは組織ごとに違うので環境変数で渡す。
    """
    c = clean(lesson_name)
    return any(kw in c for kw in SKIP_LESSON_KEYWORDS)


# 授業名の先頭に付く振替元の日付メモ（例: 8/5分 / 2025/12/17分 / 27年2/24分）。
DATE_PREFIX_RE = re.compile(r"^(?:\d{2,4}年)?\d{1,2}/\d{1,2}分")


def parse_student_name(lesson_name):
    """授業名から生徒名を取り出す。(生徒名, 推測かどうか) を返す。

    正しい形は `[生徒名]…`。まれに開き括弧が抜けた `8/5分山田太郎]…` が作られており、
    これを黙って捨てるとリマインドが届かないまま誰も気づけない（2026-08-12に発生）。
    その場合は閉じ括弧の手前を候補として拾い、推測フラグ付きで返す。
    推測した名前はLINE名簿に一致したときだけ送信する（split_unresolved_guesses）。
    """
    raw = lesson_name or ""
    m = re.search(r"\[([^\]]+)\]", raw)
    if m:
        return m.group(1).strip(), False
    # 全角の日付メモ（８／５分）も拾えるように正規化してから候補を切り出す。
    m = re.match(r"^([^\[\]]*)\]", unicodedata.normalize("NFKC", raw))
    if not m:
        return "", False
    candidate = DATE_PREFIX_RE.sub("", m.group(1)).strip()
    return (candidate, True) if candidate else ("", False)


def resolve_student_name(lesson_name, fallback_name):
    """授業名から生徒名を取る。取れなければ授業予定側の生徒名で補う。

    戻り値の2つ目は「推測かどうか」。推測はLINE名簿に一致したときだけ送信する。
    授業名を優先するのは、LINE名簿が授業名と同じ表記で登録されているため
    （例: 授業名と名簿は `渡辺翔太`、Salesforceの生徒名は `渡邊 翔太`）。
    """
    name, guessed = parse_student_name(lesson_name)
    if name:
        return name, guessed
    if fallback_name:
        return fallback_name.strip(), True
    return "", False


def fetch_student_names_by_lesson(sf, lesson_date):
    """授業Id → 生徒名 の対応を引く。授業名が壊れているときの補完用。

    MANAERP__Student_Sessions__c は授業にぶら下がる子レコードで、
    MANAERP__Student_Name__c にSalesforce上の生徒名が入っている。
    ただし全授業に付いているわけではない（対象外の枠や一部のレコードには無い）ので、
    主経路にはせず補完だけに使う。取得に失敗しても従来どおりの動作に落ちるだけ。
    """
    try:
        records = sf.query_all(
            "SELECT MANAERP__Lesson__c, MANAERP__Student_Name__c "
            "FROM MANAERP__Student_Sessions__c "
            f"WHERE MANAERP__Lesson__r.MANAERP__Lesson_Date__c = {lesson_date}"
        ).get("records", [])
    except Exception as e:
        print(f"⚠️ 授業予定の生徒名を取得できません（補完なしで続行）: {type(e).__name__}: {e}")
        return {}
    return {
        r["MANAERP__Lesson__c"]: (r.get("MANAERP__Student_Name__c") or "").strip()
        for r in records
        if r.get("MANAERP__Student_Name__c")
    }


def split_unresolved_guesses(students):
    """推測で拾った生徒名がLINE名簿に無い行を送信対象から外す。

    括弧が壊れた授業名から拾った名前は誤っている可能性があるので、LINE IDが
    引けたときだけ送る。引けなければ別人へ届くのを避けて手動送信の通知に回す。
    """
    resolved, unresolved = [], []
    for s in students:
        if s.get("_name_guessed") and not s.get("lineUserId"):
            unresolved.append(s)
        else:
            resolved.append(s)
    return resolved, unresolved


def get_offset(s):
    raw = s["コース名"]
    # 体験回に前倒しを適用すると1〜2時間早い時刻でLINE通知してしまう。
    if TRIAL_PATTERN.search(raw) and not TRIAL_EXCEPT_PATTERN.search(raw):
        return 0
    c = clean(raw)
    for pattern, offset in COURSE_RULES:
        if pattern.search(c):
            return offset
    return 0


def shift(v, o):
    if not v or o == 0:
        return v
    m = re.search(r"(\d{1,2}):(\d{2})", v)
    if not m:
        return v
    t = int(m.group(1)) * 60 + int(m.group(2)) - o * 60
    return f"{t // 60}:{t % 60:02d}"


def course_label(s):
    """通知文に出すコース名。COURSE_RULES に一致した部分だけを抜き出す。

    レコードの名称には枠や科目などの付随情報が混ざるので、判定表に載っている
    コース名の部分だけを表示する。どれにも一致しなければ名称をそのまま使う。
    """
    c = clean(s["コース名"])
    for pattern, _offset in COURSE_RULES:
        m = pattern.search(c)
        if m:
            return m.group(0)
    return clean(s["授業名"])


def get_msg(s):
    t = course_label(s)
    o = get_offset(s)
    return (
        f"{s['生徒氏名']}さん\n明日の授業の詳細です。\n"
        f"コース・教科：{t}　{s['科目']}\n"
        f"{shift(s['開始時間'], o)}‐{s['終了時間']}\n"
        f"担当：{s['担当']}\nお待ちしております。\nこの通知に返信不要です。"
    )


def fetch_report(sf):
    print("📊 授業データ取得中（SOQL）...")
    tomorrow = (datetime.now(JST) + timedelta(days=1)).strftime("%Y-%m-%d")
    records = sf.query_all(
        f"SELECT Id, Name, MANAERP__Start_Date_Time__c, MANAERP__End_Date_Time__c, "
        f"MANAERP__Teacher__c "
        f"FROM MANAERP__Lesson__c "
        f"WHERE MANAERP__Lesson_Date__c = {tomorrow} "
        f"ORDER BY MANAERP__Start_Date_Time__c"
    ).get("records", [])
    lesson_students = fetch_student_names_by_lesson(sf, tomorrow)
    students = []
    unparsed = []
    skipped = 0
    for r in records:
        lesson_name = r.get("Name", "")
        name, guessed = resolve_student_name(lesson_name, lesson_students.get(r.get("Id"), ""))
        if is_skipped_lesson(lesson_name):
            skipped += 1
            print(f"⏭️ 対象外のためスキップ: {name or '(生徒名なし)'} / {lesson_name}")
            continue
        row = {
            "生徒氏名": name,
            "開始時間": utc_to_jst(r.get("MANAERP__Start_Date_Time__c", "")),
            "終了時間": utc_to_jst(r.get("MANAERP__End_Date_Time__c", "")),
            "担当": (r.get("MANAERP__Teacher__c") or "").strip(),
            "授業名": lesson_name,
            "コース名": lesson_name,
            "科目": extract_subject(lesson_name),
            "lineUserId": "",
            "parentLineUserId": "",
            "_name_guessed": guessed,
            "_start_dt": utc_to_jst_dt(r.get("MANAERP__Start_Date_Time__c", "")),
        }
        if not name:
            # 黙って捨てるとリマインドが届かないまま気づけないので通知に回す。
            print(f"⚠️ 生徒名を授業名から取得できません: {lesson_name}")
            unparsed.append(row)
            continue
        if guessed:
            print(f"⚠️ 授業名から生徒名を確定できないため推測: {name} / {lesson_name}")
        students.append(row)
    print(f"✅ {len(students)}名取得")
    if skipped:
        print(f"⏭️ 対象外の授業を {skipped}件スキップ")
    if unparsed:
        print(f"⚠️ 生徒名を取得できない授業が {len(unparsed)}件")
    return students, unparsed


def get_first_value(item, keys):
    for key in keys:
        value = item.get(key, "")
        if value:
            return str(value).strip()
    return ""


def build_parent_line_map(data, students_data):
    """GAS/スプシから返された保護者UIDを、生徒名キーで引ける形にする。"""
    name_keys = ["name", "studentName", "生徒氏名", "生徒名", "student"]
    parent_uid_keys = [
        "parentId", "parentLineUserId", "parentLineId",
        "guardianId", "guardianLineUserId",
        "保護者UID", "保護者LINE ID", "保護者LINEUID",
        "親UID", "親LINE ID", "親LINEUID",
    ]

    parent_map = {}

    for item in students_data:
        name = get_first_value(item, name_keys)
        parent_uid = get_first_value(item, parent_uid_keys)
        name_key = normalize(name)
        if name_key in PARENT_NOTIFY_TARGET_NAMES and parent_uid:
            parent_map[name_key] = parent_uid

    for list_key in ["parents", "guardians", "parentLineIds", "parentStudents"]:
        for item in data.get(list_key, []) or []:
            name = get_first_value(item, name_keys)
            parent_uid = get_first_value(item, ["id", "lineUserId", "uid", *parent_uid_keys])
            name_key = normalize(name)
            if name_key in PARENT_NOTIFY_TARGET_NAMES and parent_uid:
                parent_map[name_key] = parent_uid

    return parent_map


def fetch_ids(retries=3):
    """GASから名簿を取得する。

    GASが一時的にJSON以外（エラーHTML等）を返すことがあり、1回失敗しただけで
    名簿が空になると全員が「LINE IDなし」に落ちて誰にも送信されない。
    そのため数回リトライする。
    """
    print("🔑 LINE ID / Slack ID取得中...")
    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            res = requests.get(GAS_URL, params={"token": ROSTER_TOKEN}, timeout=30)
            res.raise_for_status()
            data = res.json()
            students_data = data.get("students", [])
            line_map = {normalize(s.get("name", "")): s.get("id", "") for s in students_data}
            if not line_map:
                raise ValueError("GASの返却JSONに students が含まれていません")
            parent_line_map = build_parent_line_map(data, students_data)
            slack_map = {normalize(k): v for k, v in data.get("slackIds", {}).items()}
            print(
                f"✅ LINE:{len(line_map)}件 / 保護者LINE:{len(parent_line_map)}件 / Slack:{len(slack_map)}件取得"
            )
            return line_map, parent_line_map, slack_map
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            print(f"⚠️ 名簿取得失敗（{attempt}/{retries}）: {last_error}")
            if attempt < retries:
                time.sleep(3 * attempt)
    print(f"❌ 名簿取得に{retries}回失敗しました: {last_error}")
    return {}, {}, {}


def find_slack_id(teacher, slack_map):
    key = normalize(teacher)
    if key in slack_map:
        return slack_map[key], 100
    keys = list(slack_map.keys())
    if not keys:
        return "", 0
    match, score, _ = process.extractOne(key, keys, scorer=fuzz.ratio)
    if score >= 80:
        print(f"  📎 fuzzy一致: {teacher} → {match} ({score}点)")
        return slack_map[match], score
    return "", 0


def notify_slack_parent_uid_missing(students):
    if not students:
        return
    header = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"⚠️ *保護者LINE UID未登録の生徒がいます（{len(students)}名）*\n手動で保護者へLINEを送信してください。",
        },
    }
    blocks = [header]
    for s in students:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{s['生徒氏名']}（保護者宛）*\n```{get_msg(s)}```"},
        })
    try:
        requests.post(SLACK_WEBHOOK, json={"blocks": blocks}, timeout=10)
        print("✅ Slack通知送信（保護者UID未登録）")
    except Exception as e:
        print(f"⚠️ Slack通知失敗（保護者UID未登録）: {e}")


def notify_slack_no_id(students):
    if not students:
        return
    header = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"⚠️ *LINE ID未登録の生徒がいます（{len(students)}名）*\n手動でLINEを送信してください。",
        },
    }
    blocks = [header]
    for s in students:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{s['生徒氏名']}*\n```{get_msg(s)}```"}})
    try:
        requests.post(SLACK_WEBHOOK, json={"blocks": blocks}, timeout=10)
        print("✅ Slack通知送信（IDなし生徒）")
    except Exception as e:
        print(f"⚠️ Slack通知失敗: {e}")


def notify_slack_unparsed(lessons):
    """生徒を特定できなかった授業を通知する。

    授業名の `[生徒名]` が壊れていると生徒を引けない。黙って落とすと
    リマインドが届かないまま誰も気づけないので、手動送信を促す。
    """
    if not lessons:
        return
    header = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                f"⚠️ *生徒を特定できない授業があります（{len(lessons)}件）*\n"
                "授業名の `[生徒名]` が壊れている可能性があります。"
                "Salesforceの授業名を直したうえで、手動でLINEを送信してください。"
            ),
        },
    }
    blocks = [header]
    for s in lessons:
        title = f"*{s['生徒氏名']}（推測・名簿に一致なし）*\n" if s.get("生徒氏名") else ""
        detail = (
            f"授業名：{s['授業名']}\n"
            f"{s['開始時間']}‐{s['終了時間']}\n"
            f"担当：{s['担当']}"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"{title}```{detail}```"}})
    try:
        requests.post(SLACK_WEBHOOK, json={"blocks": blocks}, timeout=10)
        print("✅ Slack通知送信（生徒を特定できない授業）")
    except Exception as e:
        print(f"⚠️ Slack通知失敗（生徒を特定できない授業）: {e}")


def notify_slack_send_failed(failures):
    """LINE送信に失敗した分を通知する。

    ログには `❌` が出るがSlackに出ないと、届いていないことに誰も気づけない。
    タイムアウトの場合はGAS側で送信済みのことがあるので、再送は人が判断する。
    """
    if not failures:
        return
    header = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                f"❌ *LINEを送信できなかった授業があります（{len(failures)}件）*\n"
                "届いていない可能性があります。トーク画面を確認し、必要なら手動で送信してください。"
                "（タイムアウトの場合はGAS側で送信済みのことがあるため、二重送信に注意）"
            ),
        },
    }
    blocks = [header]
    for s, target, err in failures:
        title = f"*{s['生徒氏名']}（{target}宛）*\nエラー: {err}"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"{title}\n```{get_msg(s)}```"}})
    try:
        requests.post(SLACK_WEBHOOK, json={"blocks": blocks}, timeout=10)
        print("✅ Slack通知送信（LINE送信失敗）")
    except Exception as e:
        print(f"⚠️ Slack通知失敗（LINE送信失敗）: {e}")


def notify_slack_teacher_remind(students, slack_map):
    if not students:
        return
    by_teacher = defaultdict(list)
    for s in students:
        by_teacher[s["担当"]].append(s)
    for t in by_teacher:
        by_teacher[t].sort(key=lambda x: x["_start_dt"] or datetime.min.replace(tzinfo=JST))

    tomorrow = datetime.now(JST) + timedelta(days=1)
    date_str = tomorrow.strftime(f"%Y/%m/%d({WEEKDAY_JA[tomorrow.weekday()]})")

    lines = [f"📅 *明日の授業リマインド　{date_str}*"]
    for teacher, lessons in sorted(by_teacher.items()):
        slack_id, _score = find_slack_id(teacher, slack_map)
        mention = f"<@{slack_id}>" if slack_id else f"@{teacher}"
        lines.append(f"\n{date_str}　{mention}")
        for s in lessons:
            lines.append(f"{s['生徒氏名']}｜{s['開始時間']}｜{s['終了時間']}｜{s['担当']}")

    try:
        requests.post(SLACK_TEACHER_WEBHOOK, json={"text": "\n".join(lines)}, timeout=10)
        print("✅ Slack講師リマインド送信")
    except Exception as e:
        print(f"⚠️ Slack講師リマインド失敗: {e}")


def send(s):
    # タイムアウトしてもGAS側では送信済みのことがあるためリトライはしない。
    # 待ち時間だけ長めに取って取りこぼしを減らす。
    try:
        r = requests.post(
            GAS_URL,
            json={
                "token": ROSTER_TOKEN,
                "students": [{"lineUserId": s["lineUserId"], "name": s["生徒氏名"], "message": get_msg(s)}],
            },
            timeout=30,
        ).json()
        res = r.get("results", [{}])[0]
        return res.get("status") == "sent", res.get("error", "")
    except Exception as e:
        return False, str(e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("=" * 50)
    print("  📱 授業LINE送信ツール")
    print(f"  {datetime.now(JST).strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    print("🔐 Salesforceログイン中...")
    try:
        sf = Salesforce(username=SF_USERNAME, password=SF_PASSWORD, security_token=SF_SECURITY_TOKEN)
        print("✅ ログイン成功")
    except Exception as e:
        print(f"❌ {e}")
        sys.exit(1)

    students, unparsed = fetch_report(sf)
    line_map, parent_line_map, slack_map = fetch_ids()
    if students and not line_map:
        # 名簿が空のまま進むと全員が「LINE IDなし」になり誰にも届かない。
        # 異常終了して送信済みマーカーを残さず、次回実行で自動的に再試行させる。
        print("❌ LINE名簿を取得できませんでした。全員未送信になるのを防ぐため中止します。")
        sys.exit(1)
    for s in students:
        name_key = normalize(s["生徒氏名"])
        s["lineUserId"] = line_map.get(name_key, "")
        if name_key in PARENT_NOTIFY_TARGET_NAMES:
            s["parentLineUserId"] = parent_line_map.get(name_key, "")

    # 推測した生徒名が名簿に無い行は、別人へ届くのを避けて手動送信の通知に回す。
    students, unresolved_guesses = split_unresolved_guesses(students)
    unparsed.extend(unresolved_guesses)

    with_id = [s for s in students if s["lineUserId"]]
    without_id = [s for s in students if not s["lineUserId"]]
    with_parent_id = [s for s in with_id if s.get("parentLineUserId")]
    without_id_with_parent = [s for s in without_id if s.get("parentLineUserId")]
    parent_uid_missing = [
        s for s in students
        if normalize(s["生徒氏名"]) in PARENT_NOTIFY_TARGET_NAMES and not s.get("parentLineUserId")
    ]
    print(
        f"\n📋 送信対象: {len(with_id)}名 / 保護者同時送信: {len(with_parent_id)}名"
        f" / LINE IDなし: {len(without_id)}名 / 保護者のみ送信: {len(without_id_with_parent)}名"
        f" / 生徒を特定できず: {len(unparsed)}件\n"
    )

    sent = failed = parent_sent = parent_failed = 0
    send_failures = []
    for i, s in enumerate(with_id, 1):
        if args.dry_run:
            print(f"[DRY RUN {i}/{len(with_id)}] {s['生徒氏名']}")
            print("-" * 40)
            print(get_msg(s))
            if s.get("parentLineUserId"):
                print(f"\n[DRY RUN 保護者同時送信] {s['生徒氏名']}")
                print("-" * 40)
                print(get_msg(s))
            print()
            continue
        print(f"📤 [{i}/{len(with_id)}] {s['生徒氏名']}...", end=" ", flush=True)
        ok, err = send(s)
        if ok:
            print("✅")
            sent += 1
        else:
            print(f"❌ {err}")
            failed += 1
            send_failures.append((s, "本人", err))

        if s.get("parentLineUserId"):
            parent_s = dict(s)
            parent_s["lineUserId"] = s["parentLineUserId"]
            print(f"👪 保護者にも送信: {s['生徒氏名']}...", end=" ", flush=True)
            ok_parent, err_parent = send(parent_s)
            if ok_parent:
                print("✅")
                parent_sent += 1
            else:
                print(f"❌ {err_parent}")
                parent_failed += 1
                send_failures.append((s, "保護者", err_parent))
            time.sleep(0.3)

        time.sleep(0.3)

    for s in without_id_with_parent:
        if args.dry_run:
            print(f"\n[DRY RUN 保護者のみ送信] {s['生徒氏名']}")
            print("-" * 40)
            print(get_msg(s))
            print()
            continue
        parent_s = dict(s)
        parent_s["lineUserId"] = s["parentLineUserId"]
        print(f"👪 保護者のみ送信: {s['生徒氏名']}...", end=" ", flush=True)
        ok_parent, err_parent = send(parent_s)
        if ok_parent:
            print("✅")
            parent_sent += 1
        else:
            print(f"❌ {err_parent}")
            parent_failed += 1
            send_failures.append((s, "保護者", err_parent))
        time.sleep(0.3)

    print("\n" + "=" * 50)
    if not args.dry_run:
        print(f"  ✅ {sent}名送信完了  ❌ {failed}名失敗")
        if parent_sent or parent_failed:
            print(f"  👪 保護者LINE ✅ {parent_sent}名送信完了  ❌ {parent_failed}名失敗")
        if without_id:
            notify_slack_no_id(without_id)
        if parent_uid_missing:
            notify_slack_parent_uid_missing(parent_uid_missing)
        if unparsed:
            notify_slack_unparsed(unparsed)
        if send_failures:
            notify_slack_send_failed(send_failures)
        notify_slack_teacher_remind(students, slack_map)
    print("=" * 50)

    if without_id:
        print(f"\n⚠️ LINE IDなし {len(without_id)}名 — 手動送信してください\n")
        for s in without_id:
            print("─" * 40)
            print(f"【{s['生徒氏名']}】")
            print(get_msg(s))
            print()

    if unparsed:
        print(f"\n⚠️ 生徒を特定できない授業 {len(unparsed)}件 — 授業名を直して手動送信してください\n")
        for s in unparsed:
            print("─" * 40)
            print(f"【{s['生徒氏名'] or '生徒名不明'}】{s['授業名']}")
            print(f"{s['開始時間']}‐{s['終了時間']}　担当：{s['担当']}")
            print()


if __name__ == "__main__":
    main()
