import argparse
import json
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

# GASへは1回にまとめて渡す。GAS側の実行時間の上限は6分で、1件あたりLINE APIに
# 0.3秒ほどなので、この件数なら余裕がある。念のため上限を切って分割する。
# ただしこれは通常時の見積もりで、6分以内を保証する計算ではない。
BATCH_SIZE = 40
SEND_TIMEOUT_SEC = 180

# POSTの応答を受け取れなかったときに、GASへ結果を取りに行く間隔（秒）。
# GASがまだ送信中のこともあるので、間隔を広げながら諦めるまでの回数だけ試す。
# 全部外しても合計103秒で、ワークフローの timeout-minutes: 15 には収まる。
RESULT_RETRY_WAITS = (3, 10, 30, 60)
RESULT_FETCH_TIMEOUT_SEC = 60

# Slackは1メッセージ50 blocks・section本文3000字が上限。超えると黙って弾かれる。
SLACK_MAX_BLOCKS = 50
SLACK_MAX_TEXT = 2900
# 429で Retry-After を指定されたとき、待って投げ直してよい上限。
# これより長い指定は素直に諦める（送信処理の終了を通知のために遅らせない）。
SLACK_RETRY_AFTER_MAX_SEC = 30

# 429で締め出され、指定に従えないまま諦めた宛先。この実行中は以降の通知も送らない。
# main() は通知関数を4本続けて呼ぶので、1回の呼び出しの中でしか止めないと、
# Retry-Afterを無視して同じチャンネルへ投げ続けることになる。
# 宛先ごとに持つので、生徒向けが止まっても講師リマインドは送れる。
_SLACK_BLOCKED_WEBHOOKS = set()

# LINEがHTTPで明示的に拒否したときだけ「届いていない」と言い切れる。
# GASは UrlFetchApp の例外を code 0 の failed に変換するので（コード.js の
# makeErrorResponse）、LINEが受理した直後に応答を失っただけでも failed になる。
# code 0・5xx・code欠落を未達に混ぜると、届いているものを手動再送させてしまう。
UNDELIVERED_CODES = {400, 401, 403, 404, 429}


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
# 空文字は「未設定」として扱う。ワークフローの env に未設定のSecretを並べると
# 空文字が渡ってくるが、空の正規表現は何にでも一致するため、既定に戻さないと
# 体験回の判定が裏返る（エラーは出ず、通知時刻だけがずれる）。
TRIAL_PATTERN = re.compile(os.getenv("TRIAL_PATTERN") or "体験")
TRIAL_EXCEPT_PATTERN = re.compile(os.getenv("TRIAL_EXCEPT_PATTERN") or "体験[2２②]")

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


def slack_section(text):
    """sectionを1つ作る。text は3000字が上限なので手前で切る。"""
    if len(text) > SLACK_MAX_TEXT:
        text = _truncate_for_slack(text)
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _truncate_for_slack(text):
    """3000字の手前で切り、開いたままのコードフェンスを閉じる。

    送信本文は ``` で囲んで載せている。閉じないまま切ると以降の表示が崩れ、
    手動送信する文面を読み違える。切れ目が ``` の1文字目・2文字目に当たると
    中途半端なバッククォートが残り、開閉を数え違えるので先に落とす。
    """
    body = text[:SLACK_MAX_TEXT]
    partial = len(body) - len(body.rstrip("`"))
    if partial in (1, 2):
        body = body[:-partial]
    body += "…（省略）"
    if body.count("```") % 2 == 1:
        body += "\n```"
    return body


def _slack_pages(groups):
    """(見出し, 詳細の並び) を、1メッセージ50 blocks以下のページに割る。

    50個ずつ機械的に切ると、後半のページだけを見た人には何の通知か分からない。
    未達と結果不明が混ざったまま切れると、後続ページから「投げ直すと二重送信になる」
    という注意が消えて、届いている生徒に手動再送をかけさせてしまう。
    そのためページをまたぐグループには見出しを毎回付け直す。
    """
    pages, page = [], []
    for header, details in groups:
        if not details:
            continue
        index = 0
        while index < len(details):
            if len(page) + 2 > SLACK_MAX_BLOCKS:
                # 見出しと詳細1つぶんが入らないなら改ページする（見出しだけのページを作らない）
                pages.append(page)
                page = []
            page.append(header)
            room = SLACK_MAX_BLOCKS - len(page)
            page.extend(details[index:index + room])
            index += room
            if len(page) >= SLACK_MAX_BLOCKS:
                pages.append(page)
                page = []
    if page:
        pages.append(page)
    return pages


def _slack_retry_after(response):
    """429の Retry-After を (待ってよい秒数 or None, ヘッダーの生値) で返す。"""
    headers = getattr(response, "headers", None) or {}
    try:
        raw = headers.get("Retry-After")
    except Exception:
        raw = None
    try:
        seconds = int(str(raw).strip())
    except (TypeError, ValueError):
        return None, raw
    if 0 <= seconds <= SLACK_RETRY_AFTER_MAX_SEC:
        return seconds, raw
    return None, raw


def post_slack_payload(webhook, payload, label):
    """Incoming Webhookへ1通投げる。同じ宛先へ続けて送ってよければ True。

    Incoming Webhookの成功はHTTP 200 かつ本文が `ok` のときだけ（JSON APIの
    「200だが ok:false」とは別物）。ここを見ないと弾かれた通知を送れたつもりで見逃す。

    ここで例外を投げないのは、通知の失敗で送信処理ごと落とすと日次マーカーが残らず、
    次の実行で全員に再送されるため。気づく手段はログに寄せる。
    """
    if webhook in _SLACK_BLOCKED_WEBHOOKS:
        # 宛先URLは秘密なので出さない。どの通知が送れていないかはlabelで分かる。
        print(f"⚠️ Slack通知を送っていません（{label}）: "
              "レート制限のため、この実行では同じ宛先への通知を止めています")
        return False
    for attempt in (1, 2):
        try:
            res = requests.post(webhook, json=payload, timeout=10)
        except Exception as e:
            print(f"⚠️ Slack通知失敗（{label}）: {e}")
            return True
        code = getattr(res, "status_code", None)
        body = str(getattr(res, "text", "") or "")
        if code == 200 and body.strip() == "ok":
            print(f"✅ Slack通知送信（{label}）")
            return True
        if code != 429:
            print(f"⚠️ Slack通知を拒否されました（{label}・HTTP {code}）: {body[:200]}")
            return True
        # 429は待てば通る。ただし待つのは1回だけで、指定に従えないなら投げ直さない。
        # 同じチャンネルには他の送信元もいるので、逆らって投げると締め出しが伸びる。
        if attempt == 2:
            _SLACK_BLOCKED_WEBHOOKS.add(webhook)
            print(f"⚠️ Slack通知がレート制限のままです（{label}）。"
                  "この実行では同じ宛先への通知を止めます")
            return False
        wait, raw = _slack_retry_after(res)
        if wait is None:
            _SLACK_BLOCKED_WEBHOOKS.add(webhook)
            print(f"⚠️ Slack通知がレート制限（{label}・Retry-After: {raw}）。"
                  "指定に従えないので、この実行では同じ宛先への通知を止めます")
            return False
        print(f"⚠️ Slack通知がレート制限（{label}）。{wait}秒待って1回だけ送り直します")
        time.sleep(wait)
    return True


def post_slack_groups(groups, label):
    """見出し付きのまとまりをSlackへ投げる。50 blocksを超えるぶんは分割する。"""
    pages = _slack_pages(groups)
    for i, page in enumerate(pages):
        if not post_slack_payload(SLACK_WEBHOOK, {"blocks": page}, label):
            remaining = len(pages) - i - 1
            if remaining:
                print(f"⚠️ Slack通知の残り{remaining}ページを送っていません（{label}）")
            return
        if i + 1 < len(pages):
            # Incoming Webhookの目安は1チャンネルあたり毎秒1通（Slack公式）。
            time.sleep(1)


def notify_slack_parent_uid_missing(students):
    if not students:
        return
    header = slack_section(
        f"⚠️ *保護者LINE UID未登録の生徒がいます（{len(students)}名）*\n手動で保護者へLINEを送信してください。"
    )
    details = [slack_section(f"*{s['生徒氏名']}（保護者宛）*\n```{get_msg(s)}```") for s in students]
    post_slack_groups([(header, details)], "保護者UID未登録")


def notify_slack_no_id(students):
    if not students:
        return
    header = slack_section(
        f"⚠️ *LINE ID未登録の生徒がいます（{len(students)}名）*\n手動でLINEを送信してください。"
    )
    details = [slack_section(f"*{s['生徒氏名']}*\n```{get_msg(s)}```") for s in students]
    post_slack_groups([(header, details)], "IDなし生徒")


def notify_slack_unparsed(lessons):
    """生徒を特定できなかった授業を通知する。

    授業名の `[生徒名]` が壊れていると生徒を引けない。黙って落とすと
    リマインドが届かないまま誰も気づけないので、手動送信を促す。
    """
    if not lessons:
        return
    header = slack_section(
        f"⚠️ *生徒を特定できない授業があります（{len(lessons)}件）*\n"
        "授業名の `[生徒名]` が壊れている可能性があります。"
        "Salesforceの授業名を直したうえで、手動でLINEを送信してください。"
    )
    details = []
    for s in lessons:
        title = f"*{s['生徒氏名']}（推測・名簿に一致なし）*\n" if s.get("生徒氏名") else ""
        detail = (
            f"授業名：{s['授業名']}\n"
            f"{s['開始時間']}‐{s['終了時間']}\n"
            f"担当：{s['担当']}"
        )
        details.append(slack_section(f"{title}```{detail}```"))
    post_slack_groups([(header, details)], "生徒を特定できない授業")


def notify_slack_send_failed(failures):
    """LINE送信の結果を通知する。要素は (授業, 宛先ラベル, 理由, 未達が確定したか)。

    ログには `❌` が出るがSlackに出ないと、届いていないことに誰も気づけない。
    ただし「送れなかった」と「結果を確認できなかった」は別物。2026-09-10に14件を
    一律「送信できなかった」として流したが、GASの実行ログでは全部送信済みだった。
    毎日オオカミ少年をやると本当の未達を見落とすので、2つを分けて出す。
    """
    if not failures:
        return

    def details(items):
        return [
            slack_section(f"*{s['生徒氏名']}（{target}宛）*\nエラー: {err}\n```{get_msg(s)}```")
            for s, target, err, _confirmed in items
        ]

    undelivered = [f for f in failures if f[3]]
    unknown = [f for f in failures if not f[3]]

    # 見出しと詳細を組にして渡す。分割されても各ページに見出しが再掲されるので、
    # 後続ページだけを見た人が未達と結果不明を取り違えることがない。
    groups = []
    if undelivered:
        groups.append((slack_section(
            f"❌ *LINEを送信できませんでした（{len(undelivered)}件）*\n"
            "LINEが受け取りを断りました。トーク画面を確認し、手動で送信してください。"
        ), details(undelivered)))
    if unknown:
        groups.append((slack_section(
            f"⚠️ *LINEの送信結果を確認できませんでした（{len(unknown)}件）*\n"
            "GASは受け取っていれば最後まで送るので、届いていることが多いです。"
            "投げ直すと二重送信になるため、トーク画面を見てから判断してください。"
        ), details(unknown)))
    post_slack_groups(groups, "LINE送信失敗")


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

    post_slack_payload(SLACK_TEACHER_WEBHOOK, {"text": "\n".join(lines)}, "講師リマインド")


def _check_result_alignment(chunk, got):
    """送った並びと返ってきた並びが食い違っていないかを見る。合っていれば空文字。

    並びがずれても、送信先と本文の組み合わせは狂わない（GASへは宛先と本文を組にして
    渡していて、GASもその組で送るため）。狂うのは成功／失敗の割り当てだけだが、
    そのまま通知すると届いている生徒に手動再送をかけることになる。
    ずれを見つけても並べ替えや再送はしない。判断を人に返す。
    """
    if len(got) != len(chunk):
        return f"結果の件数が合いません（送信{len(chunk)}件 / 応答{len(got)}件）"
    for (lesson, _label, _uid), res in zip(chunk, got):
        if not isinstance(res, dict):
            return "応答の形式が想定と違います"
        name = res.get("name")
        if not name:
            # GASは成功・失敗・catchのどの枝でも name を返し、こちらが渡す name も常に非空。
            # 無いということはこの送信経路の応答ではないので、並びを突き合わせられない。
            return "結果に生徒名がありません"
        if name != lesson["生徒氏名"]:
            return f"結果の並びが送信順と違います（{lesson['生徒氏名']} のはずが {name}）"
    return ""


def _classify_result(res, label):
    """1件ぶんの結果を (ok, 理由, 未達が確定したか) にする。

    未達を言い切れるのはLINEがHTTPで断ったときだけ。GASは UrlFetchApp の例外を
    code 0 の failed に変換するので、LINEが受理した直後に応答を失っただけでも
    failed で返ってくる。これを未達に混ぜると、届いているものを手動再送させてしまう。

    GASが返す形は3つだけ。
      成功        : {name, status:'sent', recipient}
      LINEが非200 : {name, status:'failed', recipient, error, code}
      GAS側の例外 : {name, status:'failed', error}   ← recipient も code も付かない
    どれにも当てはまらない形は「結果を確認できなかった」に寄せる。
    """
    recipient = res.get("recipient")
    expected = "parent" if label == "保護者" else "student"
    if recipient is not None and recipient != expected:
        # GASは宛先UIDがlineシートE列にあるかで生徒/保護者を決める。同じUIDが
        # 生徒欄と保護者欄の両方に入っていると、並びが正しくてもここが食い違う。
        # 並びのずれとは限らないので、chunk全体ではなくこの1件だけを結果不明にする。
        return False, f"宛先の種別が合いません（{label}宛のはずが {recipient}）", False
    status = res.get("status")
    if status == "sent":
        if recipient is None:
            # 成功の枝は必ず recipient を付ける。無い成功はこの送信経路の応答ではない。
            return False, f"結果を確認できませんでした（{label}宛の成功応答に宛先の種別がありません）", False
        return True, "", True
    reason = res.get("error") or json.dumps(res, ensure_ascii=False)[:200]
    code = res.get("code")
    # code だけで決めると status:'queued' + code:400 まで未達確定になる。
    # GASが failed と言い、かつLINEがHTTPで断ったコードが付いているときに限る。
    # recipient も要る。LINEが非200を返した枝は必ず recipient を付けるので、
    # 無いのに拒否codeだけ付いた応答は契約の外。未達と言い切らずに人へ返す。
    if status == "failed" and recipient == expected and isinstance(code, int) and code in UNDELIVERED_CODES:
        return False, reason, True
    return False, reason, False


def _new_request_id(index):
    """この送信を指す使い捨ての名前。GAS側が結果を控えるときのキーになる。

    実行日時を頭に付けるのは、GASのプロパティを覗いたときにいつのものか分かるようにするため。
    """
    return f"{datetime.now(JST).strftime('%Y%m%d%H%M%S')}-{os.urandom(4).hex()}-{index}"


def fetch_saved_send_results(request_id):
    """GASに控えてある送信結果を取りに行く。拾えなければ None。

    POSTの応答はGoogleのウェブアプリ層を通るので、GASが送り終えていてもこちらに
    届かないことがある（2026-09-13の12:07、29件・53秒の実行で応答が doGet の
    unauthorized に化けた）。結果はGAS側に残っているので取りに行く。

    引き取れなかったことは「届いていない」を意味しない。GASに届いていない可能性も
    同じだけ残るので、呼び出し側は従来どおり「結果を確認できなかった」に落とす。
    """
    for wait in RESULT_RETRY_WAITS:
        # GASがまだ送信中のことがあるので、1回目から間を置く。
        time.sleep(wait)
        try:
            body = requests.get(
                GAS_URL,
                params={"token": ROSTER_TOKEN, "action": "lessonSendResult", "requestId": request_id},
                timeout=RESULT_FETCH_TIMEOUT_SEC,
            ).json()
        except Exception as e:
            print(f"   …結果の引き取りに失敗（{e}）", flush=True)
            continue
        if not isinstance(body, dict):
            continue
        # 別のidの控えを読むと、前回の実行の結果を今回の結果として扱ってしまう。
        if str(body.get("requestId") or "") != request_id:
            continue
        results = body.get("results")
        if isinstance(results, list):
            print(f"   …GASに控えた結果を引き取れました（{len(results)}件）", flush=True)
            return results
    return None


def _read_send_results(chunk, body, request_id=None, failure_note=""):
    """GASの応答を chunk と同じ長さの (ok, 理由, 未達が確定したか) の並びにする。

    応答から結果を読めなかったときは、GASに控えた結果を引き取りに行く（request_id がある場合）。
    """
    n = len(chunk)
    if n == 0:
        # 0件の日に投げる「実行した記録」用のPOST。読む結果が無いので引き取りにも行かない。
        return []
    got = body.get("results") if isinstance(body, dict) else None
    if not isinstance(got, list) and request_id:
        print("⚠️ 送信の応答から結果を読めませんでした。GASに控えた結果を取りに行きます", flush=True)
        got = fetch_saved_send_results(request_id)
    if not isinstance(got, list):
        # POSTが302の追従でGETに化けて doGet が動くと、合言葉をURLクエリで見るため
        # {"error":"unauthorized"} が返る。中身を捨てるとSlackのエラー欄が空欄になり
        # 原因が追えなくなる（2026-09-10に14件中5件がこれだった）。
        if failure_note:
            return [(False, f"結果を確認できませんでした（{failure_note}）", False)] * n
        note = json.dumps(body, ensure_ascii=False)[:200] if body is not None else "空の応答"
        return [(False, f"結果を確認できませんでした（想定外の応答: {note}）", False)] * n

    misaligned = _check_result_alignment(chunk, got)
    if misaligned:
        return [(False, f"結果を確認できませんでした（{misaligned}）", False)] * n

    return [_classify_result(res, label) for (_lesson, label, _uid), res in zip(chunk, got)]


def send_all(targets):
    """[(授業, 宛先ラベル, LINE UID), ...] をまとめてGASへ渡す。

    1名1POSTだと往復のたびにGoogleのウェブアプリ層で詰まる余地ができる。
    2026-09-10は29往復のうち14回がそれで「失敗」になった（LINEは全部届いていた）。
    往復回数そのものが事故の確率なので、1回にまとめる。

    タイムアウトしてもリトライはしない。GAS側は受け取った時点で最後まで送るので、
    投げ直すと二重送信になる。応答を受け取れなかったときは投げ直さず、
    GASが控えた結果を requestId で引き取りに行く。
    """
    outcomes = []
    chunks = [targets[start:start + BATCH_SIZE] for start in range(0, len(targets), BATCH_SIZE)]
    # 対象が0件の日も1回だけ投げる。GASは students が空なら誰にも送らないが、
    # 「今日の実行がGASまで届いた」記録が残る。これが無いと、受け取る側の見張りから
    # 授業が0件だった日と止まった日を見分けられない（2026-09-14）。
    if not chunks:
        chunks = [[]]
    for index, chunk in enumerate(chunks):
        request_id = _new_request_id(index)
        payload = [
            {"lineUserId": uid, "name": lesson["生徒氏名"], "message": get_msg(lesson)}
            for lesson, _label, uid in chunk
        ]
        body = None
        failure_note = ""
        try:
            body = requests.post(
                GAS_URL,
                json={"token": ROSTER_TOKEN, "requestId": request_id, "students": payload},
                timeout=SEND_TIMEOUT_SEC,
            ).json()
        except Exception as e:
            # 投げ直さない。GASは受け取っていれば送り終えているので、結果は引き取りに行く。
            failure_note = str(e)
        outcomes.extend(_read_send_results(chunk, body, request_id, failure_note))
    return outcomes


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

    # 本人と保護者を1本の並びにする。GASへはこの順のまま渡し、同じ順で結果が返る。
    targets = []
    for s in with_id:
        targets.append((s, "本人", s["lineUserId"]))
        if s.get("parentLineUserId"):
            targets.append((s, "保護者", s["parentLineUserId"]))
    for s in without_id_with_parent:
        targets.append((s, "保護者", s["parentLineUserId"]))

    if args.dry_run:
        for i, (s, label, _uid) in enumerate(targets, 1):
            print(f"[DRY RUN {i}/{len(targets)}] {s['生徒氏名']}（{label}宛）")
            print("-" * 40)
            print(get_msg(s))
            print()
        outcomes = []
    else:
        print(f"📤 {len(targets)}件をまとめて送信中...", flush=True)
        outcomes = send_all(targets)

    sent = failed = parent_sent = parent_failed = 0
    send_failures = []
    for (s, label, _uid), (ok, err, confirmed) in zip(targets, outcomes):
        mark = "📤" if label == "本人" else "👪"
        print(f"{mark} {s['生徒氏名']}（{label}宛）... " + ("✅" if ok else f"❌ {err}"))
        if ok:
            if label == "本人":
                sent += 1
            else:
                parent_sent += 1
        else:
            if label == "本人":
                failed += 1
            else:
                parent_failed += 1
            send_failures.append((s, label, err, confirmed))

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
