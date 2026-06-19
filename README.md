# lesson-reminder

> Salesforceの授業データを元に、毎朝LINEと Slackで翌日の特訓リマインドを自動送信する業務自動化スクリプト

塾の現場で毎日発生していた「翌日の授業リマインドをLINEで手動送信する」作業を完全自動化。Salesforceから取得した授業情報をもとに、生徒へLINE・講師へSlackへ自動通知します。GitHub Actionsで毎朝11時に実行されます。

## 解決した課題

| 項目 | Before | After |
|---|---|---|
| リマインド送信 | 担当者が毎日手動でLINE送信 | 毎朝11時に全員へ自動送信 |
| LINE IDなし生徒 | 把握できず漏れが発生 | Slackへ自動通知・手動送信リストを出力 |
| 講師への連絡 | 別途Slackに手動投稿 | 担当講師のSlackメンション付きで自動通知 |
| 保護者への連絡 | 個別に手動送信 | 対象生徒を指定して自動送信 |

## 背景・導入経緯

授業の前日に担当者が手動で生徒全員に LINE リマインドを送っていた。授業データは Salesforce に蓄積されているにもかかわらず、それをコピーして手動で LINE を作成・送信するという作業が毎日発生していた。LINE ID が未登録の生徒は漏れが生じやすく、講師への連絡も別途 Slack に手動投稿が必要だった。

Salesforce のデータを直接参照して自動化することで、毎日の手作業をゼロにした。GitHub Actions で確実に定刻実行される仕組みにすることで、担当者が毎日意識しなくても通知が届く運用を実現している。

## 技術スタック

![Python](https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white)
![Salesforce](https://img.shields.io/badge/Salesforce-00A1E0?style=flat&logo=salesforce&logoColor=white)
![LINE](https://img.shields.io/badge/LINE-00C300?style=flat&logo=line&logoColor=white)
![Slack](https://img.shields.io/badge/Slack-4A154B?style=flat&logo=slack&logoColor=white)
![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-2088FF?style=flat&logo=github-actions&logoColor=white)

- **Python 3.11+**
- **Salesforce REST API** (`simple-salesforce`) — 翌日の授業データをSOQLで取得
- **LINE Messaging API** — 生徒・保護者へのプッシュ通知（GAS経由）
- **Slack Incoming Webhook** — LINE IDなし生徒の通知・講師向けリマインド
- **Google Sheets API** — 保護者LINE IDの管理
- **GitHub Actions** — `workflow_dispatch` で外部cronから起動

## アーキテクチャ

```
外部cron（毎朝 11:00 JST）→ GitHub API（workflow_dispatch）
  ↓
tokkun_reminder_runner.py
  ├─ Salesforce SOQL → 翌日の授業一覧取得
  ├─ GAS Webアプリ  → 生徒・講師のLINE/Slack IDを取得
  ├─ Google Sheets  → 保護者LINE IDを取得（任意）
  │
  ├─ LINE IDあり生徒 → LINE Push送信（GAS経由）
  ├─ 保護者通知対象  → 保護者LINEへも同時送信
  ├─ LINE IDなし生徒 → Slackへ手動送信リストを通知
  └─ 全講師         → Slack メンション付きでリマインド送信
```

## 実装上の工夫

- **fuzzy一致**（rapidfuzz）でSlack IDを検索し、Salesforceの講師名表記ゆれに対応
- **冪等性の担保**: `.tokkun-reminder-state/` に送信済みマーカーを記録し、同日の二重送信を防止
- **`--dry-run` モード**: LINE/Slack への送信なしで取得内容と送信文面だけ確認可能
- **研修スキップ**: 特訓名に「研修」を含む授業は自動的にリマインド対象外
- **保護者通知**: `PARENT_NOTIFY_TARGET_NAMES` 環境変数で通知対象の生徒を管理（コードに名前を書かない設計）

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
python tokkun_reminder_runner.py --dry-run
```

## GitHub Actionsでの運用

リポジトリの **Settings → Secrets and variables → Actions** に以下を登録してください。

| Secret | 説明 |
|---|---|
| `SF_USERNAME` | Salesforce ユーザー名 |
| `SF_PASSWORD` | Salesforce パスワード |
| `SF_SECURITY_TOKEN` | Salesforce セキュリティトークン |
| `GAS_URL` | GAS WebアプリのデプロイURL |
| `SLACK_WEBHOOK` | Slack Incoming Webhook URL（管理者通知用） |
| `SLACK_TEACHER_WEBHOOK` | Slack Incoming Webhook URL（講師リマインド用） |
| `CREDENTIALS_JSON` | Google サービスアカウントJSON（文字列） |
| `PARENT_LINE_SPREADSHEET_ID` | 保護者LINE IDを管理するスプレッドシートID（任意） |
| `PARENT_NOTIFY_TARGET_NAMES` | 保護者にも通知する生徒名（カンマ区切り、任意） |

**定期実行**: GitHub ActionsのスケジュールトリガーはPublicリポジトリで不安定なため、外部cronサービス（[cron-job.org](https://cron-job.org) 等）からGitHub APIの `workflow_dispatch` エンドポイントを叩いて毎朝起動しています。

```bash
# 外部cronから叩くAPIコール例
curl -X POST \
  -H "Authorization: Bearer YOUR_GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/YOUR_USERNAME/lesson-reminder/actions/workflows/tokkun-reminder.yml/dispatches \
  -d '{"ref":"master","inputs":{"dry_run":"false"}}'
```

**手動実行**: Actions → Tokkun Reminder → Run workflow
- `dry_run=true`: 送信せずログ確認のみ
- `dry_run=false`: 本番送信

## 問い合わせデータ同期（main.py）

問い合わせ管理ポータルからCSVをダウンロードし、Googleスプレッドシートへ自動転記するスクリプトです。重複（電話番号・氏名）を自動検出しスキップします。

```bash
python main.py
```

GitHub Actions から手動実行する場合は `inquiry-sync.yml` ワークフローを使用してください。

追加で必要な Secrets:

| Secret | 説明 |
|---|---|
| `PORTAL_EMAIL` | ポータルサイトのログインメール |
| `PORTAL_PASSWORD` | ポータルサイトのパスワード |
| `LOGIN_URL` | ポータルサイトのログインURL |
| `SPREADSHEET_ID` | 転記先スプレッドシートID |
| `SCHOOL_NAME` | 校舎名でCSVをフィルタ（任意） |
| `INQUIRY_SOURCE_LABEL` | 問い合わせ経路のラベル（任意） |

## ファイル構成

```
lesson-reminder/
├── tokkun_reminder.py          # Salesforce取得・LINE/Slack送信のコア処理
├── tokkun_reminder_runner.py   # ラッパー（研修スキップ・保護者LINE統合）
├── main.py                     # 問い合わせCSVダウンロード→スプレッドシート転記
├── requirements.txt
├── .env.example
├── .gitignore
└── .github/workflows/
    ├── tokkun-reminder.yml     # 毎朝 11:00 JST 自動実行
    └── inquiry-sync.yml        # 手動実行（問い合わせ同期）
```

## ライセンス

MIT
