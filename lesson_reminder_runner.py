"""Runner for lesson reminders.

This wraps the main reminder script and normalizes Salesforce lesson names before
message generation. It keeps the original script small while adding operational
cleanup rules.
"""

import json
import os
import re
import unicodedata
from urllib.parse import quote

import requests

import lesson_reminder

PARENT_LINE_SPREADSHEET_ID = os.getenv("PARENT_LINE_SPREADSHEET_ID", "")
PARENT_LINE_SHEET_NAME = os.getenv("PARENT_LINE_SHEET_NAME", "line")
# H列（保護者通知のチェックボックス）まで読む。列を増やしたらここも広げること。
PARENT_LINE_SHEET_COLUMNS = "A:H"

COL_PARENT_UID = 4          # E列: 保護者UID
COL_PARENT_STUDENT = 6      # G列: その保護者に対応する生徒フルネーム
COL_PARENT_NOTIFY = 7       # H列: 保護者通知のチェックボックス

# チェックボックスは TRUE / FALSE で返るが、手で印を入れられても拾えるようにしておく。
CHECKED_VALUES = {"TRUE", "1", "✓", "☑", "○", "◯"}

# 保護者通知の対象。実体は名簿シートH列のチェックで決まり、
# fetch_ids_with_parent_sheet() の中で apply_parent_targets() が入れ替える。
PARENT_NOTIFY_TARGET_NAMES = set()


def apply_parent_targets(targets):
    """保護者通知の対象を確定させ、本体側の同名定数にも反映する。

    lesson_reminder.build_parent_line_map() と main() がこの集合を見るので、
    名簿を取りに行く前に呼ぶこと。
    """
    global PARENT_NOTIFY_TARGET_NAMES
    PARENT_NOTIFY_TARGET_NAMES = set(targets)
    lesson_reminder.PARENT_NOTIFY_TARGET_NAMES = PARENT_NOTIFY_TARGET_NAMES
    return PARENT_NOTIFY_TARGET_NAMES


def is_parent_notify_checked(value):
    """H列の値がチェック済みか判定する。

    Sheets APIはチェックボックスを UNFORMATTED_VALUE では bool、
    FORMATTED_VALUE では "TRUE"/"FALSE" の文字列で返す。どちらでも同じ結果になるようにする。
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().upper() in CHECKED_VALUES


def parse_parent_rows(rows):
    """名簿シートの行から (通知対象の生徒名, 生徒名 → 保護者UID) を作る。

    H列にチェックが入っている行だけを対象にする。チェックがあるのに保護者UIDが
    空の行は、対象には入れるが map には入れない。こうしておくと本体側が
    「保護者LINE UID未登録」としてSlackに出すので、設定漏れが表に出る。
    """
    targets, parent_map = set(), {}
    for row in rows[1:]:
        def cell(index):
            # 末尾の空セルは省略されて返るため、長さを確かめてから取る。
            return str(row[index]).strip() if len(row) > index else ""

        student_name = cell(COL_PARENT_STUDENT)
        checked = is_parent_notify_checked(
            row[COL_PARENT_NOTIFY] if len(row) > COL_PARENT_NOTIFY else ""
        )
        if not checked or not student_name:
            continue
        name_key = lesson_reminder.normalize(student_name)
        targets.add(name_key)
        parent_uid = cell(COL_PARENT_UID)
        if parent_uid:
            parent_map[name_key] = parent_uid
    return targets, parent_map


def clean_lesson_name(value):
    text = unicodedata.normalize("NFKC", value or "").strip()
    text = re.sub(r"^(?:\d{2,4}年)?\d{1,2}/\d{1,2}分", "", text).strip()
    text = re.sub(r"^\[[^\]]*\]", "", text).strip()
    return text


_original_fetch_report = lesson_reminder.fetch_report
_original_fetch_ids = lesson_reminder.fetch_ids


def fetch_report_with_extra_filter(sf):
    """Apply SKIP_LESSON_KEYWORDS again after this runner's stricter cleanup.

    The base module already filters, but the runner also strips a leading date
    prefix from lesson names. A second pass catches rows whose keyword only
    becomes visible after that extra normalization.
    """
    students, unparsed = _original_fetch_report(sf)
    filtered = []
    skipped = 0
    for student in students:
        lesson_name = clean_lesson_name(student.get("授業名", ""))
        if lesson_reminder.is_skipped_lesson(lesson_name):
            skipped += 1
            print(f"Skip lesson: {student.get('生徒氏名', '')} / {lesson_name}")
            continue
        filtered.append(student)
    if skipped:
        print(f"Skipped lessons: {skipped}")
    return filtered, unparsed


def fetch_sheet_rows_with_api(credentials):
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession

    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = service_account.Credentials.from_service_account_info(credentials, scopes=scopes)
    session = AuthorizedSession(creds)
    range_name = f"{PARENT_LINE_SHEET_NAME}!{PARENT_LINE_SHEET_COLUMNS}"
    encoded_range = quote(range_name, safe="")
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{PARENT_LINE_SPREADSHEET_ID}"
        f"/values/{encoded_range}?valueRenderOption=UNFORMATTED_VALUE"
    )
    response = session.get(url, timeout=20)
    if not response.ok:
        print(f"⚠️ Sheets API status: {response.status_code}")
        print(f"⚠️ Sheets API body: {response.text[:500]}")
        response.raise_for_status()
    return response.json().get("values", [])


def fetch_parent_settings_from_sheet():
    """名簿シートから保護者通知の設定を読む。

    Sheet layout (configurable via PARENT_LINE_SHEET_NAME):
    - A: student LINE UID
    - C: student name
    - E: parent LINE UID
    - G: student name corresponding to the parent UID
    - H: parent notification checkbox

    戻り値は (対象の生徒名, 生徒名 → 保護者UID, エラー文字列)。
    エラー時は空で返し、呼び出し側がSlackに出す。
    """
    if not PARENT_LINE_SPREADSHEET_ID:
        return set(), {}, "PARENT_LINE_SPREADSHEET_ID が未設定"

    credentials_json = os.getenv("CREDENTIALS_JSON", "").strip()
    if not credentials_json:
        return set(), {}, "CREDENTIALS_JSON が未設定"

    try:
        credentials = json.loads(credentials_json)
    except Exception as e:
        return set(), {}, f"CREDENTIALS_JSON のJSON解析失敗: {type(e).__name__}"

    client_email = credentials.get("client_email", "")
    if client_email:
        print(f"🔐 Googleサービスアカウント: {client_email}")

    try:
        rows = fetch_sheet_rows_with_api(credentials)
    except Exception as e:
        return set(), {}, f"名簿シートを取得できません: {type(e).__name__}: {e}"

    targets, parent_map = parse_parent_rows(rows)
    print(f"✅ 保護者通知チェック:{len(targets)}名 / うちUIDあり:{len(parent_map)}名")
    return targets, parent_map, ""


def notify_parent_sheet_problem(message):
    """保護者通知の設定を読めなかったことをSlackに出す。

    黙って0件に落ちると保護者へのリマインドが止まったまま誰も気づけない。
    """
    print(f"⚠️ {message}")
    try:
        requests.post(
            lesson_reminder.SLACK_WEBHOOK,
            json={"text": f"⚠️ *保護者リマインドの設定を読めませんでした*\n{message}"},
            timeout=10,
        )
    except Exception as e:
        print(f"⚠️ Slack通知失敗（保護者通知の設定）: {type(e).__name__}: {e}")


def fetch_ids_with_parent_sheet():
    if not PARENT_LINE_SPREADSHEET_ID:
        # 保護者通知を使わない構成。設定が無いこと自体は異常ではないので鳴らさない。
        print("ℹ️ PARENT_LINE_SPREADSHEET_ID が未設定のため、保護者への通知は行いません")
        apply_parent_targets(set())
        return _original_fetch_ids()

    # シートを先に読んで対象を確定させる。本体の build_parent_line_map() が
    # この集合でGAS側の保護者UIDを絞るため、名簿取得より後ろにはできない。
    targets, sheet_parent_map, error = fetch_parent_settings_from_sheet()
    apply_parent_targets(targets)

    line_map, gas_parent_map, slack_map = _original_fetch_ids()
    parent_line_map = dict(sheet_parent_map)
    if gas_parent_map:
        print("✅ GAS返却JSONの保護者LINEで上書き")
        parent_line_map.update(gas_parent_map)

    if error:
        notify_parent_sheet_problem(
            f"{error}\n今回の実行では保護者へのリマインドは送られていません。"
        )
    elif not targets:
        notify_parent_sheet_problem(
            "名簿シートH列（保護者通知）のチェックが1件も入っていません。"
            "設定が消えていないか確認してください。"
        )

    print(
        f"✅ 保護者LINE統合後:{len(parent_line_map)}件 "
        f"/ 対象:{','.join(sorted(PARENT_NOTIFY_TARGET_NAMES))}"
    )
    return line_map, parent_line_map, slack_map


lesson_reminder.clean = clean_lesson_name
lesson_reminder.fetch_report = fetch_report_with_extra_filter
lesson_reminder.fetch_ids = fetch_ids_with_parent_sheet


if __name__ == "__main__":
    lesson_reminder.main()
