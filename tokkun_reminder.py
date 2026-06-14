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
SLACK_WEBHOOK = required_env("SLACK_WEBHOOK")
SLACK_TEACHER_WEBHOOK = required_env("SLACK_TEACHER_WEBHOOK")


def normalize(s):
    return unicodedata.normalize("NFKC", s).replace(" ", "").replace("　", "").strip()


def _load_parent_notify_targets() -> set:
    """
    環境変数 PARENT_NOTIFY_TARGET_NAMES からカンマ区切りで生徒名を読み込む。
    例: PARENT_NOTIFY_TARGET_NAMES=山田太郎,佐藤花子
    未設定の場合は空のセットを返す（保護者へのLINE通知なし）。
    """
    raw = os.getenv("PARENT_NOTIFY_TARGET_NAMES", "")
    return {normalize(n.strip()) for n in raw.split(",") if n.strip()}


PARENT_NOTIFY_TARGET_NAMES: set = _load_parent_notify_targets()


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


def is_undecided_tokkun(tokkun_name):
    """特訓名の接頭に「未定」がある、または「研修」を含む場合はリマインド対象外にする。"""
    c = clean(tokkun_name)
    return c.startswith("未定") or "研修" in c


def get_offset(s):
    c = clean(s["コース名"])
    if re.search(r"個別管理特訓L", c):
        return 2
    if re.search(r"個別管理特訓S", c):
        return 1
    return 0


def shift(v, o):
    if not v or o == 0:
        return v
    m = re.search(r"(\d{1,2}):(\d{2})", v)
    if not m:
        return v
    t = int(m.group(1)) * 60 + int(m.group(2)) - o * 60
    return f"{t // 60}:{t % 60:02d}"


def get_msg(s):
    c = clean(s["コース名"])
    m = re.search(r"(個別管理特訓[LS]|完全指導特訓[LS]|独学支援特訓[LS]?|宿題確認特訓[LS]?|体験特訓)", c)
    t = m.group(1) if m else clean(s["特訓名"])
    o = get_offset(s)
    return (
        f"{s['生徒氏名']}さん\n明日の特訓の詳細です。\n"
        f"コース・教科：{t}　{s['科目']}\n"
        f"{shift(s['開始時間'], o)}‐{s['終了時間']}\n"
        f"担当：{s['担当']}\nお待ちしております。\nこの通知に返信不要です。"
    )


def fetch_report(sf):
    print("📊 特訓データ取得中（SOQL）...")
    tomorrow = (datetime.now(JST) + timedelta(days=1)).strftime("%Y-%m-%d")
    records = sf.query_all(
        f"SELECT Name, MANAERP__Start_Date_Time__c, MANAERP__End_Date_Time__c, "
        f"MANAERP__Teacher__c "
        f"FROM MANAERP__Lesson__c "
        f"WHERE MANAERP__Lesson_Date__c = {tomorrow} "
        f"ORDER BY MANAERP__Start_Date_Time__c"
    ).get("records", [])
    students = []
    skipped_undecided = 0
    for r in records:
        tokkun = r.get("Name", "")
        m = re.search(r"\[([^\]]+)\]", tokkun)
        name = m.group(1).strip() if m else ""
        if not name:
            continue
        if is_undecided_tokkun(tokkun):
            skipped_undecided += 1
            print(f"⏭️ 未定のためスキップ: {name} / {tokkun}")
            continue
        students.append(
            {
                "生徒氏名": name,
                "開始時間": utc_to_jst(r.get("MANAERP__Start_Date_Time__c", "")),
                "終了時間": utc_to_jst(r.get("MANAERP__End_Date_Time__c", "")),
                "担当": (r.get("MANAERP__Teacher__c") or "").strip(),
                "特訓名": tokkun,
                "コース名": tokkun,
                "科目": extract_subject(tokkun),
                "lineUserId": "",
                "parentLineUserId": "",
                "_start_dt": utc_to_jst_dt(r.get("MANAERP__Start_Date_Time__c", "")),
            }
        )
    print(f"✅ {len(students)}名取得")
    if skipped_undecided:
        print(f"⏭️ 未定の特訓を {skipped_undecided}件スキップ")
    return students


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


def fetch_ids():
    print("🔑 LINE ID / Slack ID取得中...")
    try:
        data = requests.get(GAS_URL, timeout=15).json()
        students_data = data.get("students", [])
        line_map = {normalize(s.get("name", "")): s.get("id", "") for s in students_data}
        parent_line_map = build_parent_line_map(data, students_data)
        slack_map = {normalize(k): v for k, v in data.get("slackIds", {}).items()}
        print(
            f"✅ LINE:{len(line_map)}件 / 保護者LINE:{len(parent_line_map)}件 / Slack:{len(slack_map)}件取得"
        )
        return line_map, parent_line_map, slack_map
    except Exception as e:
        print(f"⚠️ {e}")
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
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{s['生徒氏名']}（保護者宛）*\n```{get_msg(s)}```"}})
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

    lines = [f"📅 *明日の特訓リマインド　{date_str}*"]
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
    try:
        r = requests.post(
            GAS_URL,
            json={"students": [{"lineUserId": s["lineUserId"], "name": s["生徒氏名"], "message": get_msg(s)}]},
            timeout=15,
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
    print("  📱 特訓LINE送信ツール")
    print(f"  {datetime.now(JST).strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    print("🔐 Salesforceログイン中...")
    try:
        sf = Salesforce(username=SF_USERNAME, password=SF_PASSWORD, security_token=SF_SECURITY_TOKEN)
        print("✅ ログイン成功")
    except Exception as e:
        print(f"❌ {e}")
        sys.exit(1)

    students = fetch_report(sf)
    line_map, parent_line_map, slack_map = fetch_ids()
    for s in students:
        name_key = normalize(s["生徒氏名"])
        s["lineUserId"] = line_map.get(name_key, "")
        if name_key in PARENT_NOTIFY_TARGET_NAMES:
            s["parentLineUserId"] = parent_line_map.get(name_key, "")

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
        f" / LINE IDなし: {len(without_id)}名 / 保護者のみ送信: {len(without_id_with_parent)}名\n"
    )

    sent = failed = parent_sent = parent_failed = 0
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
        notify_slack_teacher_remind(students, slack_map)
    print("=" * 50)

    if without_id:
        print(f"\n⚠️ LINE IDなし {len(without_id)}名 — 手動送信してください\n")
        for s in without_id:
            print("─" * 40)
            print(f"【{s['生徒氏名']}】")
            print(get_msg(s))
            print()


if __name__ == "__main__":
    main()
