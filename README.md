# lesson-reminder

> Salesforceの授業データを元に、翌日の授業リマインドをLINEとSlackへ自動送信する業務自動化スクリプト

毎日発生していた「翌日の授業リマインドを手動でLINE送信する」作業を自動化。Salesforceから取得した授業情報をもとに、生徒へLINE・担当講師へSlackを送ります。GitHub Actions上で毎日12:07（JST）に動いています。

2026年5月から毎日動かしており、このリポジトリには**その運用の中で壊れた箇所の修正が入っています**。詳しくは「運用して分かったこと」を参照してください。

## 解決した課題

| 項目 | Before | After |
|---|---|---|
| リマインド送信 | 担当者が毎日手動でLINE送信 | 毎日12:07に全員へ自動送信 |
| LINE IDなしの生徒 | 把握できず漏れが発生 | Slackへ自動通知・手動送信用の文面を出力 |
| 講師への連絡 | 別途Slackに手動投稿 | 担当講師のSlackメンション付きで自動通知 |
| 保護者への連絡 | 個別に手動送信 | 対象者を指定して自動送信 |
| 送信の失敗 | 誰も気づかず放置 | Slackへ即時通知し、次の実行で自動リトライ |

## 背景・導入経緯

授業の前日に担当者が手動で生徒全員にLINEリマインドを送っていた。授業データはSalesforceに蓄積されているにもかかわらず、それをコピーして手動でLINEを作成・送信する作業が毎日発生していた。LINE IDが未登録の人は漏れが生じやすく、講師への連絡も別途Slackに手動投稿が必要だった。

Salesforceのデータを直接参照して自動化することで、毎日の手作業をゼロにした。GitHub Actionsで定刻実行される仕組みにすることで、担当者が毎日意識しなくても通知が届く運用にしている。

## 技術スタック

![Python](https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white)
![Salesforce](https://img.shields.io/badge/Salesforce-00A1E0?style=flat&logo=salesforce&logoColor=white)
![LINE](https://img.shields.io/badge/LINE-00C300?style=flat&logo=line&logoColor=white)
![Slack](https://img.shields.io/badge/Slack-4A154B?style=flat&logo=slack&logoColor=white)
![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-2088FF?style=flat&logo=github-actions&logoColor=white)

- **Python 3.11+**
- **Salesforce REST API**（`simple-salesforce`）— 翌日の授業データをSOQLで取得
- **LINE Messaging API** — 生徒・保護者へのプッシュ通知（GAS経由）
- **Slack Incoming Webhook** — LINE IDなしの通知・講師向けリマインド・失敗通知
- **Google Sheets API** — 保護者LINE IDの管理
- **GitHub Actions** — `workflow_dispatch` で外部cronから起動
- **pytest** — コース判定ロジックのユニットテスト

## アーキテクチャ

```
外部cron（毎日 3:07 UTC = 12:07 JST）→ GitHub API（workflow_dispatch）
  ↓
lesson_reminder_runner.py
  ├─ Salesforce SOQL → 翌日の授業一覧を取得
  ├─ GAS Webアプリ  → 生徒のLINE ID・講師のSlack IDを取得（合言葉付き・3回リトライ）
  ├─ Google Sheets  → 保護者LINE IDを取得（任意）
  │
  ├─ LINE IDあり → LINE Push送信（GAS経由）
  ├─ 保護者通知対象 → 保護者LINEへも同時送信
  ├─ LINE IDなし → Slackへ手動送信用の文面を通知
  └─ 全講師      → Slack メンション付きで翌日の担当一覧を送信
  ↓
送信済みマーカーをコミット（同日の二重送信を防ぐ）
  ↓
失敗した場合 → Slackへ通知し、マーカーを残さず次回実行で自動リトライ
```

## 実装上の工夫

- **fuzzy一致**（rapidfuzz）でSlack IDを検索し、Salesforce側の講師名の表記ゆれに対応
- **冪等性の担保**: `.lesson-reminder-state/` に送信済みマーカーをコミットし、同日の二重送信を防止
- **失敗したら送らない**: 名簿が取れなければ送信せず異常終了する。中途半端に送るより、送らずに気づける状態にする
- **`--dry-run` モード**: LINE/Slackへ送信せず、取得内容と送信文面だけを確認できる
- **固有名をコードに書かない**: 保護者通知の対象者もコース名の判定表も環境変数から読み込む。組織ごとに違う値をコードに残さない

## コース名の判定表

コース名は組織ごとに違うため、`COURSE_RULES` 環境変数で外から渡します。書式は `正規表現:前倒し時間` のカンマ区切りで、前から順に評価して最初に一致したものを採用します。

```bash
COURSE_RULES=標準コースL:2,標準コースS:1,短期コース:0
```

「前倒し時間」は、コースによって実際の開始時刻がレコード上の時刻よりN時間早いという運用差を吸収するためのものです。通知文に出すコース名もこの表から決まります。

体験回だけは前倒しが発生しないため、`TRIAL_PATTERN`（既定 `体験`）に一致し、かつ `TRIAL_EXCEPT_PATTERN`（既定 `体験[2２②]`）に一致しないものは前倒しなしとして扱います。

リマインド対象外にしたい授業は `SKIP_LESSON_KEYWORDS` にカンマ区切りで指定します（担当がまだ決まっていない仮枠、生徒が関与しない社内向けの枠など）。

## 運用して分かったこと

毎日動かしている中で実際に起きた事故と、その対策です。**どれもコードのロジックのバグではなく、外部サービスの挙動と実行環境の側にありました。**

**1. 名簿が取れないと、失敗せずに「全員LINE IDなし」で終わる。**
GASが一時的にJSON以外（エラーHTML等）を返すと、例外を握りつぶして空の名簿を返していました。全員が「LINE IDなし」に落ちて誰にも届かないのに、ワークフローは成功扱いで終わり、送信済みマーカーまで記録されるので翌日まで気づけません。名簿取得を3回リトライし、それでも空なら `exit 1` して**マーカーを残さない**ようにしました。マーカーが無ければ次の実行が自動的にリトライになります。

**2. GAS側に認証を足したら、翌朝から届かなくなった。**
GASのWebアプリはURLを知っていれば誰でも叩けるので、スクリプトプロパティの合言葉（`ROSTER_TOKEN`）で保護するようにしました。ところが呼び出し側にトークンを渡していなかったため、翌朝のリマインドが名簿を取得できず全員未送信になりました。1の対策が効いて誤送信にはなりませんでしたが、**APIを保護するときは呼び出し側を同時に直す**必要があります。

**3. 失敗しても誰も気づかない。**
上の2件は、どちらも人が手で気づくまで放置されていました。GitHub Actionsの失敗はメールが飛ぶだけで、運用者が見る場所には出てきません。失敗時にSlackへ通知するステップを追加し、疎通確認用に `test_notify` 入力で通知だけを1回送れるようにしました。

**4. ランナーの遅延で二重送信が起きかける。**
`actions/checkout` は既定でディスパッチ時点のSHAを取得します。ランナーが混んで実行開始が遅れると、先行runがpushした送信済みマーカーが見えないまま起動し、二重送信になりえます。`ref` に実行時点のブランチを指定して最新を取得し、マーカーのpush前に `git pull --rebase` を入れて競合で落ちないようにしました。

**5. 体験回だけ通知時刻が1〜2時間早かった。**
コース名から前倒し時間を決めていましたが、体験回は同じコース名でも実際の開始が前倒しになりません。コース名だけで判断していたため、体験回に誤った時刻を通知していました。

## セットアップ

### 1. 依存パッケージのインストール

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 環境変数の設定

```bash
cp .env.example .env
# .env に実値を入れる
```

### 3. ローカルでのテスト実行

```bash
python lesson_reminder_runner.py --dry-run
```

### 4. テスト

```bash
pytest tests/ -q
```

コース名の判定表・体験回の扱い・除外キーワードを、Salesforceへ接続せずに検証します。

## GitHub Actionsでの運用

リポジトリの **Settings → Secrets and variables → Actions** に以下を登録してください。

| Secret | 説明 |
|---|---|
| `SF_USERNAME` | Salesforce ユーザー名 |
| `SF_PASSWORD` | Salesforce パスワード |
| `SF_SECURITY_TOKEN` | Salesforce セキュリティトークン |
| `GAS_URL` | GAS WebアプリのデプロイURL |
| `ROSTER_TOKEN` | GAS側の `doGet`/`doPost` を守る合言葉 |
| `SLACK_WEBHOOK` | Slack Incoming Webhook URL（管理者通知・失敗通知用） |
| `SLACK_TEACHER_WEBHOOK` | Slack Incoming Webhook URL（講師リマインド用） |
| `CREDENTIALS_JSON` | Google サービスアカウントJSON（文字列） |
| `PARENT_LINE_SPREADSHEET_ID` | 保護者LINE IDを管理するスプレッドシートID（任意） |
| `PARENT_NOTIFY_TARGET_NAMES` | 保護者にも通知する対象者名（カンマ区切り、任意） |
| `COURSE_RULES` | コース名の判定表（任意） |

**定期実行**: GitHub Actionsのスケジュールトリガーは実行が数十分ずれることがあり、時刻に意味のある通知には使えません。ワークフローに `schedule` は置かず、外部cronサービス（[cron-job.org](https://cron-job.org) 等）からGitHub APIの `workflow_dispatch` エンドポイントを叩いて起動しています。

```bash
# 外部cronから叩くAPIコール例
curl -X POST \
  -H "Authorization: Bearer YOUR_GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/YOUR_USERNAME/lesson-reminder/actions/workflows/lesson-reminder.yml/dispatches \
  -d '{"ref":"master","inputs":{"dry_run":"false"}}'
```

**手動実行**: Actions → Lesson Reminder → Run workflow

- `dry_run=true`: 送信せずログ確認のみ
- `dry_run=false`: 本番送信
- `test_notify=true`: 失敗していなくてもSlackの失敗通知を1回送る（通知の疎通確認用）

## 問い合わせデータ同期（main.py）

問い合わせ管理ポータルからCSVをダウンロードし、Googleスプレッドシートへ自動転記するスクリプトです。重複（電話番号・氏名）を自動検出しスキップします。

```bash
python main.py
```

GitHub Actionsから実行する場合は `inquiry-sync.yml` ワークフローを使用してください。

追加で必要な Secrets:

| Secret | 説明 |
|---|---|
| `PORTAL_EMAIL` | ポータルサイトのログインメール |
| `PORTAL_PASSWORD` | ポータルサイトのパスワード |
| `LOGIN_URL` | ポータルサイトのログインURL |
| `SPREADSHEET_ID` | 転記先スプレッドシートID |
| `SCHOOL_NAME` | 拠点名でCSVをフィルタ（任意） |
| `INQUIRY_SOURCE_LABEL` | 問い合わせ経路のラベル（任意） |

## ファイル構成

```
lesson-reminder/
├── lesson_reminder.py          # Salesforce取得・LINE/Slack送信のコア処理
├── lesson_reminder_runner.py   # ラッパー（名称の正規化・保護者LINE統合）
├── main.py                     # 問い合わせCSVダウンロード→スプレッドシート転記
├── tests/
│   └── test_course_rules.py    # コース判定・体験回・除外キーワードのテスト
├── requirements.txt
├── .env.example
├── .gitignore
└── .github/workflows/
    ├── lesson-reminder.yml     # 外部cronからディスパッチされる本体
    └── inquiry-sync.yml        # 手動実行（問い合わせ同期）
```

## ライセンス

MIT
