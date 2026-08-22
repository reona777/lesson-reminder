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
| 保護者への連絡 | 個別に手動送信 | 名簿シートのチェックだけで対象を切り替え・自動送信 |
| 送信の失敗 | 誰も気づかず放置 | Slackへ即時通知し、次の実行で自動リトライ |
| 授業名が壊れた1件 | 無言で消えて気づけない | 名簿と照合して補完・確定できなければSlackへ通知 |

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
- **pytest** — コース判定・生徒名の解決・保護者通知設定のユニットテスト（73件）

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
  ├─ 生徒を特定できず → Slackへ通知（送らない）
  ├─ 送信に失敗   → Slackへ通知（本人／保護者を区別）
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
- **固有名をコードに書かない**: コース名の判定表も除外キーワードも環境変数から読み込む。保護者通知の対象は名簿シートのチェックで決まり、コードにもSecretにも名前を置かない
- **推測した宛先には送らない**: 授業名が壊れていて生徒名を補完した場合、LINE名簿の氏名と完全に一致したときだけ送信する。届かないより誤配のほうが害が大きい
- **設定を読めなければ止めて知らせる**: 保護者通知の設定が読めない、またはチェックが0件のときは送信を止めてSlackに出す。0件が「意図的」なのか「設定が消えた」のかは区別できない

## コース名の判定表

コース名は組織ごとに違うため、`COURSE_RULES` 環境変数で外から渡します。書式は `正規表現:前倒し時間` のカンマ区切りで、前から順に評価して最初に一致したものを採用します。

```bash
COURSE_RULES=標準コースL:2,標準コースS:1,短期コース:0
```

「前倒し時間」は、コースによって実際の開始時刻がレコード上の時刻よりN時間早いという運用差を吸収するためのものです。通知文に出すコース名もこの表から決まります。

体験回だけは前倒しが発生しないため、`TRIAL_PATTERN`（既定 `体験`）に一致し、かつ `TRIAL_EXCEPT_PATTERN`（既定 `体験[2２②]`）に一致しないものは前倒しなしとして扱います。

リマインド対象外にしたい授業は `SKIP_LESSON_KEYWORDS` にカンマ区切りで指定します（担当がまだ決まっていない仮枠、生徒が関与しない社内向けの枠など）。

## 保護者にも送る生徒の設定

**名簿スプレッドシートの H列「保護者通知」のチェックだけで決まります。** コードの書き換えもpushも不要です。

- チェックを入れるのは E〜G列（保護者UID / lineネーム / 生徒フルネーム）の行です。生徒本人のA〜C列ではありません
- チェックしたのに保護者UID（E列）が空の場合は送信されず、Slackに「保護者LINE UID未登録」として出ます
- 反映は次回の実行からです

以前は対象者の氏名をコードに直書きしていたため、希望者が増えるたびにスクリプトを書き換えてpushする必要がありました。運用の設定がデプロイを要求する状態だったということです。読み取りは `UNFORMATTED_VALUE` でチェックボックスを真偽値として受け取り、手で `✓` などを入れられていても拾えるようにしてあります。

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

**6. 授業名の括弧が1文字欠けただけで、1人分が無言で消えた。**
生徒名は授業名の `[生徒名]` から取っています。開き括弧の抜けた `8/5分山田太郎]…` というレコードが作られ、正規表現に一致せず、その1件だけリマインドが届きませんでした。**送信件数は減るのに、ログにもSlackにも何も出ません。** Salesforce側の件数とログの取得件数を突き合わせて初めて分かりました。閉じ括弧の手前を候補として拾い、さらに子オブジェクト `MANAERP__Student_Sessions__c` の生徒名で補完するようにしました。ただしどちらも推測なので、**LINE名簿の氏名と完全に一致したときだけ送信**し、一致しなければ送らずSlackへ回します。補完を主経路にしていないのは、この子オブジェクトが付いていない授業があること、氏名がSalesforceの正式表記でLINE名簿とずれる場合があること（`渡辺翔太` と `渡邊 翔太`）が理由です。

**7. ジョブが3時間半ハングし、しかも失敗通知が飛ばなかった。**
GitHub Actionsランナー既定のaptミラーが応答せず、`playwright install-deps` が3時間37分止まりました。その間の定期実行は待機のままキャンセルされ続け、同期が半日止まりました。厄介なのは対策のほうです。`timeout-minutes` で打ち切ると conclusion が `failure` ではなく `cancelled` になり、**GitHubの失敗通知は飛びません**（実測）。詰まるコマンド自体を `timeout 5m` で包み、終了コード124で「失敗」にして通知が出るようにしました。`timeout-minutes` は最後の砦として残しています。ミラー一覧そのものも本家だけに差し替えています。

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
python -m flake8 --max-line-length=120 lesson_reminder.py lesson_reminder_runner.py tests/
```

Salesforceにも GAS にも接続せずに73件を検証します。コース名の判定表・体験回の扱い・除外キーワードに加え、壊れた授業名からの生徒名の解決と、保護者通知チェックの読み取りが対象です。`pytest` と `flake8` は本番実行に不要なので `requirements.txt` には入れていません。

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
| `PARENT_LINE_SPREADSHEET_ID` | 保護者LINE IDと保護者通知チェックを管理するスプレッドシートID（任意） |
| `PARENT_LINE_SHEET_NAME` | 上記のシート名（既定 `line`・任意） |
| `COURSE_RULES` | コース名の判定表（任意） |

保護者に送る生徒はSecretsではなくシートのH列で決まります。設定変更にデプロイが要らないようにするためです。

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
│   ├── conftest.py             # import時の必須環境変数をダミーで埋める
│   ├── test_course_rules.py    # コース判定・体験回・除外キーワードのテスト
│   ├── test_student_name.py    # 壊れた授業名からの生徒名解決・失敗通知のテスト
│   └── test_parent_notify.py   # 保護者通知チェックの読み取りのテスト
├── requirements.txt
├── .env.example
├── .gitignore
└── .github/workflows/
    ├── lesson-reminder.yml     # 外部cronからディスパッチされる本体
    └── inquiry-sync.yml        # 手動実行（問い合わせ同期）
```

## ライセンス

MIT
