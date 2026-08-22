import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# lesson_reminder は import 時に required_env で必須環境変数を検査して sys.exit する。
# テストでは Salesforce にも GAS にも接続しないのでダミーを入れておく。
for key in [
    "SF_USERNAME",
    "SF_PASSWORD",
    "SF_SECURITY_TOKEN",
    "GAS_URL",
    "ROSTER_TOKEN",
    "SLACK_WEBHOOK",
    "SLACK_TEACHER_WEBHOOK",
]:
    os.environ.setdefault(key, "dummy")
