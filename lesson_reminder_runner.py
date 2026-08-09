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

import lesson_reminder

PARENT_LINE_SPREADSHEET_ID = os.getenv("PARENT_LINE_SPREADSHEET_ID", "")
PARENT_LINE_SHEET_NAME = os.getenv("PARENT_LINE_SHEET_NAME", "line")

PARENT_NOTIFY_TARGET_NAMES = lesson_reminder.PARENT_NOTIFY_TARGET_NAMES
lesson_reminder.PARENT_NOTIFY_TARGET_NAMES = PARENT_NOTIFY_TARGET_NAMES


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
    students = _original_fetch_report(sf)
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
    return filtered


def fetch_sheet_rows_with_api(credentials):
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession

    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = service_account.Credentials.from_service_account_info(credentials, scopes=scopes)
    session = AuthorizedSession(creds)
    range_name = f"{PARENT_LINE_SHEET_NAME}!A:G"
    encoded_range = quote(range_name, safe="")
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{PARENT_LINE_SPREADSHEET_ID}"
        f"/values/{encoded_range}?valueRenderOption=FORMATTED_VALUE"
    )
    response = session.get(url, timeout=20)
    if not response.ok:
        print(f"⚠️ Sheets API status: {response.status_code}")
        print(f"⚠️ Sheets API body: {response.text[:500]}")
        response.raise_for_status()
    return response.json().get("values", [])


def fetch_parent_line_map_from_sheet():
    """Read parent LINE IDs directly from the spreadsheet.

    Sheet layout (configurable via PARENT_LINE_SHEET_NAME):
    - A: student LINE UID
    - C: student name
    - E: parent LINE UID
    - G: student name corresponding to the parent UID
    """
    if not PARENT_LINE_SPREADSHEET_ID:
        print("⚠️ PARENT_LINE_SPREADSHEET_ID が未設定のため、保護者LINEをスプシから取得できません")
        return {}

    credentials_json = os.getenv("CREDENTIALS_JSON", "").strip()
    if not credentials_json:
        print("⚠️ CREDENTIALS_JSON が未設定のため、保護者LINEをスプシから取得できません")
        return {}

    try:
        credentials = json.loads(credentials_json)
    except Exception as e:
        print(f"⚠️ CREDENTIALS_JSON のJSON解析失敗: {type(e).__name__}: {repr(e)}")
        return {}

    client_email = credentials.get("client_email", "")
    if client_email:
        print(f"🔐 Googleサービスアカウント: {client_email}")

    try:
        rows = fetch_sheet_rows_with_api(credentials)
    except Exception as e:
        print(f"⚠️ 保護者LINEスプシ取得失敗: {type(e).__name__}: {repr(e)}")
        return {}

    parent_map = {}
    for row in rows[1:]:
        parent_uid = row[4].strip() if len(row) > 4 else ""
        student_name = row[6].strip() if len(row) > 6 else ""
        name_key = lesson_reminder.normalize(student_name)
        if name_key in PARENT_NOTIFY_TARGET_NAMES and parent_uid:
            parent_map[name_key] = parent_uid

    print(f"✅ 保護者LINEスプシ取得:{len(parent_map)}件")
    return parent_map


def fetch_ids_with_parent_sheet():
    line_map, parent_line_map, slack_map = _original_fetch_ids()
    if parent_line_map:
        print("✅ GAS返却JSONから保護者LINEを取得済み")
    else:
        sheet_parent_map = fetch_parent_line_map_from_sheet()
        parent_line_map.update(sheet_parent_map)
    print(f"✅ 保護者LINE統合後:{len(parent_line_map)}件")
    return line_map, parent_line_map, slack_map


lesson_reminder.clean = clean_lesson_name
lesson_reminder.fetch_report = fetch_report_with_extra_filter
lesson_reminder.fetch_ids = fetch_ids_with_parent_sheet


if __name__ == "__main__":
    lesson_reminder.main()
