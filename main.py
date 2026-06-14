import os
import re
from datetime import datetime

import gspread
import pandas as pd
from dateutil.relativedelta import relativedelta
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

DOWNLOAD_PATH = "/tmp/downloads"
CREDENTIALS_PATH = "credentials.json"

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
INQUIRY_SHEET_NAME = os.getenv("INQUIRY_SHEET_NAME", "問合")
SCHOOL_NAME = os.getenv("SCHOOL_NAME", "")
INQUIRY_SOURCE_LABEL = os.getenv("INQUIRY_SOURCE_LABEL", "")
LOGIN_URL = os.getenv("LOGIN_URL", "")


def get_required_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"環境変数 {name} が設定されていません。GitHub Secrets を確認してください。")
    return value


def run():
    email = get_required_env("PORTAL_EMAIL")
    password = get_required_env("PORTAL_PASSWORD")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        page.goto(LOGIN_URL)
        page.get_by_role("textbox", name="メールアドレス").fill(email)
        page.get_by_role("textbox", name="パスワード").fill(password)
        page.get_by_role("button", name="ログイン").click()
        page.wait_for_load_state("networkidle")

        page.get_by_text("対応済").click()
        page.wait_for_timeout(1000)

        with page.expect_download() as download_info:
            page.get_by_role("button", name=" CSVデータを出力").click()
        download = download_info.value

        os.makedirs(DOWNLOAD_PATH, exist_ok=True)
        file_path = os.path.join(DOWNLOAD_PATH, "inquiry_latest.csv")
        download.save_as(file_path)
        print("ダウンロード完了:", file_path)

        browser.close()
        return file_path


def normalize_grade(raw):
    if not raw:
        return ""

    s = str(raw).strip()
    s = "".join(chr(ord(c) - 0xFEE0) if "０" <= c <= "９" else c for c in s)
    s = re.sub(r"^[新現旧]", "", s)

    if re.search(r"高卒|既卒|浪人", s):
        return "既卒"

    m = re.search(r"中学[校生]?\s*([1-3一二三])年?", s)
    if m:
        return f"中学{to_arabic(m.group(1))}年"

    m = re.search(r"高[校学等]+[校生]?\s*([1-3一二三])年?", s)
    if m:
        return f"高校{to_arabic(m.group(1))}年"

    m = re.search(r"小学[校生]?\s*([1-6一二三四五六])年?", s)
    if m:
        return f"小学{to_arabic(m.group(1))}年"

    return s


def to_arabic(c):
    mapping = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
    if c in mapping:
        return mapping[c]
    return int(c)


def format_phone(phone):
    digits = "".join(filter(str.isdigit, phone))

    if not digits:
        return phone

    if re.match(r"^(090|080|070|060|050)", digits) and len(digits) == 11:
        return f"{digits[0:3]}-{digits[3:7]}-{digits[7:11]}"

    if re.match(r"^(03|06)", digits) and len(digits) == 10:
        return f"{digits[0:2]}-{digits[2:6]}-{digits[6:10]}"

    if re.match(r"^0", digits) and len(digits) == 10:
        return f"{digits[0:4]}-{digits[4:7]}-{digits[7:10]}"

    return digits


def transfer_to_spreadsheet(file_path, gc):
    try:
        df = pd.read_csv(file_path, encoding="cp932")
    except Exception:
        df = pd.read_csv(file_path)

    if SCHOOL_NAME:
        df = df[df["教室名"] == SCHOOL_NAME]
        print(f"=== {SCHOOL_NAME}の全件 ===")
    else:
        print("=== 全件 ===")

    print(df[["生徒氏名（姓）", "生徒氏名（名）", "資料請求日"]].to_string())

    df["資料請求日"] = pd.to_datetime(df["資料請求日"], errors="coerce")

    today = datetime.today()
    this_month = today.replace(day=1)
    last_month = this_month - relativedelta(months=1)

    df = df[df["資料請求日"] >= last_month]

    print(f"対象期間：{last_month.strftime('%Y/%m')} 〜 {this_month.strftime('%Y/%m')}　該当件数：{len(df)}件")
    print(df[["生徒氏名（姓）", "生徒氏名（名）", "資料請求日"]].to_string())

    df = df.drop_duplicates(subset=["生徒氏名（姓）", "生徒氏名（名）"], keep="first")
    df["資料請求日"] = df["資料請求日"].dt.strftime("%Y/%m/%d")
    df = df.fillna("")

    sh = gc.open_by_key(SPREADSHEET_ID)
    sheet = sh.worksheet(INQUIRY_SHEET_NAME)

    all_values = sheet.get_all_values()

    existing_phones = set()
    existing_names = set()

    for row in all_values[1:]:
        if len(row) > 23:
            phone = "".join(filter(str.isdigit, str(row[23])))
            if phone:
                existing_phones.add(phone)

        if len(row) > 8:
            name = str(row[8]).strip()
            if name:
                existing_names.add(name)

    last_row = 2
    for i in range(len(all_values) - 1, 1, -1):
        if len(all_values[i]) > 8 and str(all_values[i][8]).strip() != "":
            last_row = i + 1
            break

    added = 0
    skipped = 0

    for _, row in df.iterrows():
        last_name = str(row["生徒氏名（姓）"]).strip()
        first_name = str(row["生徒氏名（名）"]).strip()

        if not last_name:
            continue

        full_name = f"{last_name} {first_name}"

        raw_phone = str(row.get("電話番号", "")).strip().replace('"', "")
        normalized_phone = "".join(filter(str.isdigit, raw_phone))
        raw_phone = format_phone(raw_phone)

        if normalized_phone and normalized_phone in existing_phones:
            print(f"  スキップ（電話重複）: {full_name} / {normalized_phone}")
            skipped += 1
            continue

        if full_name.strip() and full_name.strip() in existing_names:
            print(f"  スキップ（氏名重複）: {full_name}")
            skipped += 1
            continue

        kana_last = str(row.get("フリガナ（姓）", "")).strip()
        kana_first = str(row.get("フリガナ（名）", "")).strip()
        full_kana = f"{kana_last} {kana_first}"

        grade = normalize_grade(row.get("学年", ""))
        email = str(row.get("メールアドレス", "")).strip()

        full_address = " ".join(
            filter(
                None,
                [
                    str(row.get("郵便番号", "")).strip().replace('"', ""),
                    str(row.get("都道府県", "")).strip(),
                    str(row.get("市区町村", "")).strip(),
                    str(row.get("丁目・番地・号", "")).strip().replace('"', ""),
                    str(row.get("建物名・部屋番号", "")).strip(),
                ],
            )
        )

        inquiry_date = str(row.get("資料請求日", "")).strip()
        target_row = last_row + added + 1

        sheet.format(f"X{target_row}", {"numberFormat": {"type": "TEXT"}})

        OFFSET = 3
        col_count = 24
        new_row = [""] * col_count

        new_row[3 - OFFSET] = inquiry_date
        new_row[8 - OFFSET] = full_name
        new_row[9 - OFFSET] = full_kana
        new_row[11 - OFFSET] = grade
        new_row[13 - OFFSET] = INQUIRY_SOURCE_LABEL
        new_row[14 - OFFSET] = "資料請求"
        new_row[23 - OFFSET] = raw_phone
        new_row[25 - OFFSET] = email
        new_row[26 - OFFSET] = full_address

        sheet.update([new_row], f"D{target_row}:AA{target_row}")

        existing_phones.add(normalized_phone)
        existing_names.add(full_name.strip())
        added += 1

    print(f"転記完了！ ✅ 追記：{added}件 ⏭ スキップ（重複）：{skipped}件")


if __name__ == "__main__":
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=scopes)
    gc = gspread.authorize(creds)

    file_path = run()
    transfer_to_spreadsheet(file_path, gc)
