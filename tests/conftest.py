import os
import sys

import pytest

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


@pytest.fixture(autouse=True)
def reset_slack_block_state():
    """Slackのレート制限で止めた宛先はプロセスに残る。テスト間へ持ち越さない。"""
    import lesson_reminder

    lesson_reminder._SLACK_BLOCKED_WEBHOOKS.clear()
    yield
    lesson_reminder._SLACK_BLOCKED_WEBHOOKS.clear()


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """待ち時間を実際に待たない。

    Slackの429待ちと送信結果の引き取り（3・10・30・60秒）が本物のまま走ると、
    テスト全体が数分単位で伸びる。待ち方そのものを確かめたいテストは、
    テスト側で time.sleep を差し替えれば後勝ちでそちらが効く。
    """
    import lesson_reminder

    monkeypatch.setattr(lesson_reminder.time, "sleep", lambda s: None)
