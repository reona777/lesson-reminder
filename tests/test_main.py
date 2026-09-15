"""main() を dry-run なしで通す統合テスト。

外部依存（Salesforce・GAS・Slack）だけを差し替えて、本番と同じ経路を通す。
単体テストでは見えない次の3つを見る。

- GASへ同じchunkを投げ直していないこと（投げ直すと二重送信になる）
- 本人と保護者の結果が入れ替わらず、集計が合うこと
- 送信後の通知が失敗しても main() から例外が漏れないこと
  （例外で落ちると日次マーカーが残らず、次の実行で全員に再送される）
"""

import importlib.util
import os
import sys

import pytest


def _load_isolated_lesson_reminder():
    """このテスト専用に lesson_reminder をもう1つ読み込む。

    `lesson_reminder_runner` は import しただけで `lesson_reminder` の
    clean / fetch_report / fetch_ids を差し替える。同じpytestの実行で
    runner のテストが先に読み込まれると、素の main() を通せなくなる。
    別名で読み込んだこのコピーには runner の差し替えが届かないので、
    収集順に依存しない。`sys.modules` には入れないため、
    reload と違って他のテストへ副作用が漏れることもない。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "lesson_reminder_isolated_for_main_test",
        os.path.join(root, "lesson_reminder.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lr = _load_isolated_lesson_reminder()

GAS = "https://script.example/exec"
SLACK = "https://hooks.slack.example/services/failed"
TEACHER = "https://hooks.slack.example/services/teacher"


@pytest.fixture(autouse=True)
def _reset_isolated_slack_block_state():
    """conftest のfixtureは本物のモジュールしか掃除しない。こちらの分も消す。"""
    lr._SLACK_BLOCKED_WEBHOOKS.clear()
    yield
    lr._SLACK_BLOCKED_WEBHOOKS.clear()


class _SlackRes:
    def __init__(self, status_code=200, text="ok", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class _JsonRes:
    """GASのPOST応答。body に例外クラスを渡すと json() で送出する。"""

    def __init__(self, body):
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def raise_for_status(self):
        return None


class _FakeSalesforce:
    def __init__(self, lessons, sessions=None, **kwargs):
        self._lessons = lessons
        self._sessions = sessions or []

    def query_all(self, soql):
        if "Student_Sessions" in soql:
            return {"records": self._sessions}
        return {"records": self._lessons}


def _lesson_record(name, index=0, teacher="中澤 健吾"):
    hour = 8 + index % 6
    return {
        "Id": f"a0{index}",
        "Name": f"[{name}]標準コースS(英):指導枠",
        "MANAERP__Start_Date_Time__c": f"2026-09-11T{hour:02d}:00:00Z",
        "MANAERP__End_Date_Time__c": f"2026-09-11T{hour + 1:02d}:00:00Z",
        "MANAERP__Teacher__c": teacher,
    }


def _roster(names, parents=()):
    students = [{"id": f"U{i}", "name": n} for i, n in enumerate(names, 1)]
    return {
        "students": students,
        "parents": [{"id": f"P{i}", "name": n} for i, n in enumerate(parents, 1)],
        "slackIds": {"中澤 健吾": "U0SLACK"},
    }


class _Harness:
    """main() の外側だけを差し替えて動かす。"""

    def __init__(self, monkeypatch, names, *, parents=(), gas=None, slack=None, sessions=None,
                 saved=None):
        self.gas_posts = []
        self.slack_posts = []
        # GAS側が doPost の結果を控える挙動。指定しなければ控えなし（引き取りは空振りする）。
        self._saved_of = saved
        self.saved_results = {}
        self._gas = gas or (lambda payload: _JsonRes({"results": [
            {"name": s["name"], "status": "sent", "recipient": "student"}
            for s in payload["students"]
        ]}))
        self._slack = slack or (lambda url, payload: _SlackRes())

        lessons = [_lesson_record(n, i) for i, n in enumerate(names)]
        monkeypatch.setattr(lr, "Salesforce", lambda **kw: _FakeSalesforce(lessons, sessions))
        monkeypatch.setattr(lr, "GAS_URL", GAS)
        monkeypatch.setattr(lr, "SLACK_WEBHOOK", SLACK)
        monkeypatch.setattr(lr, "SLACK_TEACHER_WEBHOOK", TEACHER)
        monkeypatch.setattr(lr, "PARENT_NOTIFY_TARGET_NAMES",
                            {lr.normalize(n) for n in parents})
        monkeypatch.setattr(lr.time, "sleep", lambda s: None)
        monkeypatch.setattr(sys, "argv", ["lesson_reminder.py"])

        roster = _roster(names, parents)

        def fake_get(url, params=None, timeout=None):
            params = params or {}
            # 名簿取得と送信結果の引き取りは同じ doGet に来る。action で分かれるところまで本物に合わせる。
            if params.get("action") == "lessonSendResult":
                rid = params.get("requestId")
                if rid in self.saved_results:
                    return _JsonRes({"requestId": rid, "results": self.saved_results[rid]})
                return _JsonRes({"requestId": None, "results": None})
            return _JsonRes(roster)

        monkeypatch.setattr(lr.requests, "get", fake_get)
        monkeypatch.setattr(lr.requests, "post", self._post)

    def _post(self, url, json=None, timeout=None):
        if url == GAS:
            self.gas_posts.append(json)
            # GASは応答を返す前に控える。応答が化けても控えは残る、という順序をここでも守る。
            if self._saved_of and json.get("requestId"):
                self.saved_results[json["requestId"]] = self._saved_of(json["students"])
            return self._gas(json)
        self.slack_posts.append((url, json))
        return self._slack(url, json)

    @property
    def sent_uids(self):
        return [[s["lineUserId"] for s in p["students"]] for p in self.gas_posts]

    @property
    def failure_texts(self):
        return [
            b["text"]["text"]
            for url, payload in self.slack_posts if url == SLACK
            for b in payload.get("blocks", [])
        ]


class TestMainHappyPath:
    def test_全員に届けば失敗通知を出さない(self, monkeypatch, capsys):
        h = _Harness(monkeypatch, ["山田太郎", "高橋美咲"])
        lr.main()
        out = capsys.readouterr().out

        assert h.sent_uids == [["U1", "U2"]], "1回のPOSTにまとめる"
        assert "✅ 2名送信完了" in out
        assert "❌ 0名失敗" in out
        assert h.failure_texts == [], "失敗が無ければ通知しない"
        assert any(url == TEACHER for url, _ in h.slack_posts), "講師リマインドは出す"

    def test_保護者にも送ると本人と保護者を分けて数える(self, monkeypatch, capsys):
        def gas(payload):
            return _JsonRes({"results": [
                {"name": s["name"], "status": "sent",
                 "recipient": "parent" if s["lineUserId"].startswith("P") else "student"}
                for s in payload["students"]
            ]})

        h = _Harness(monkeypatch, ["山田太郎", "高橋美咲"], parents=["高橋美咲"], gas=gas)
        lr.main()
        out = capsys.readouterr().out

        assert h.sent_uids == [["U1", "U2", "P1"]], "本人・保護者の順で1本の並びにする"
        assert "✅ 2名送信完了" in out
        assert "👪 保護者LINE ✅ 1名送信完了" in out

    def test_授業が0件の日も実行した記録を残す(self, monkeypatch):
        """GAS側の見張りが、止まった日と0件の日を見分けられるようにするため。"""
        h = _Harness(monkeypatch, [])
        lr.main()

        assert len(h.gas_posts) == 1, "0件でも1回は投げる"
        assert h.gas_posts[0]["students"] == [], "誰にも送らせない"
        assert h.gas_posts[0]["requestId"]
        assert h.failure_texts == []

    def test_本人が失敗しても保護者の成功と入れ替わらない(self, monkeypatch, capsys):
        def gas(payload):
            results = []
            for s in payload["students"]:
                if s["lineUserId"] == "U2":
                    results.append({"name": s["name"], "status": "failed", "recipient": "student",
                                    "error": "ユーザーにブロックされています", "code": 403})
                else:
                    results.append({"name": s["name"], "status": "sent",
                                    "recipient": "parent" if s["lineUserId"].startswith("P")
                                    else "student"})
            return _JsonRes({"results": results})

        h = _Harness(monkeypatch, ["山田太郎", "高橋美咲"], parents=["高橋美咲"], gas=gas)
        lr.main()
        out = capsys.readouterr().out

        assert "✅ 1名送信完了  ❌ 1名失敗" in out
        assert "👪 保護者LINE ✅ 1名送信完了  ❌ 0名失敗" in out
        texts = "\n".join(h.failure_texts)
        assert "高橋美咲（本人宛）" in texts
        assert "高橋美咲（保護者宛）" not in texts, "保護者は届いているので手動再送させない"
        assert "送信できませんでした" in texts


class TestMainGasTrouble:
    def test_タイムアウトしても同じchunkを投げ直さない(self, monkeypatch, capsys):
        def gas(payload):
            raise RuntimeError("Read timed out. (read timeout=180)")

        h = _Harness(monkeypatch, ["山田太郎", "高橋美咲"], gas=gas)
        lr.main()
        out = capsys.readouterr().out

        assert h.sent_uids == [["U1", "U2"]], "投げ直すと二重送信になる"
        texts = "\n".join(h.failure_texts)
        assert "確認できませんでした" in texts
        assert "二重送信" in texts
        assert "送信できませんでした" not in texts, "未達と言い切らない"
        assert "❌ 2名失敗" in out

    def test_JSONで返ってこなくても落ちない(self, monkeypatch):
        def gas(payload):
            return _JsonRes(ValueError("Expecting value: line 1 column 1 (char 0)"))

        h = _Harness(monkeypatch, ["山田太郎"], gas=gas)
        lr.main()

        texts = "\n".join(h.failure_texts)
        assert "確認できませんでした" in texts
        assert "Expecting value" in texts

    def test_unauthorizedが返っても理由を残す(self, monkeypatch):
        """控えも引き取れなかった場合。理由を捨てるとSlackのエラー欄が空欄になる。"""
        h = _Harness(monkeypatch, ["山田太郎"],
                     gas=lambda payload: _JsonRes({"error": "unauthorized"}))
        lr.main()

        texts = "\n".join(h.failure_texts)
        assert "unauthorized" in texts
        assert "送信できませんでした" not in texts

    def test_応答が化けても控えを引き取れれば通知を出さない(self, monkeypatch, capsys):
        """2026-09-13の再現。GASは29件とも送り終えていたのに全件が通知に乗った。

        応答が化けただけで結果はGAS側に残っているので、引き取って成功と分かれば
        Slackには何も出さない。ここが出続けると本当の未達が埋もれる。
        """
        h = _Harness(
            monkeypatch, ["山田太郎", "高橋美咲"],
            gas=lambda payload: _JsonRes({"error": "unauthorized"}),
            saved=lambda students: [
                {"name": s["name"], "status": "sent", "recipient": "student"} for s in students
            ],
        )
        lr.main()
        out = capsys.readouterr().out

        assert "✅ 2名送信完了  ❌ 0名失敗" in out
        assert h.failure_texts == [], "届いているので通知は要らない"
        assert h.sent_uids == [["U1", "U2"]], "引き取りに行くだけで、投げ直さない"

    def test_控えに失敗が混ざっていればその1件だけ通知する(self, monkeypatch):
        """引き取った結果でも成功と失敗の選り分けは変わらない。"""
        def saved(students):
            return [
                {"name": s["name"], "status": "sent", "recipient": "student"}
                if s["name"] == "山田太郎" else
                {"name": s["name"], "status": "failed", "recipient": "student",
                 "error": "ユーザーにブロックされています", "code": 403}
                for s in students
            ]

        h = _Harness(monkeypatch, ["山田太郎", "高橋美咲"],
                     gas=lambda payload: _JsonRes({"error": "unauthorized"}),
                     saved=saved)
        lr.main()

        texts = "\n".join(h.failure_texts)
        assert "高橋美咲（本人宛）" in texts
        assert "山田太郎" not in texts
        assert "送信できませんでした" in texts, "LINEが403で断ったので未達は確定している"

    def test_件数がずれたchunkは全員を結果不明にする(self, monkeypatch):
        h = _Harness(monkeypatch, ["山田太郎", "高橋美咲"],
                     gas=lambda payload: _JsonRes({"results": [
                         {"name": "山田太郎", "status": "sent", "recipient": "student"}]}))
        lr.main()

        texts = "\n".join(h.failure_texts)
        assert "件数が合いません" in texts
        assert "高橋美咲" in texts

    def test_41件は40と1に分かれ1つ目が失敗しても2つ目を送る(self, monkeypatch, capsys):
        names = [f"生徒{i:02d}" for i in range(41)]
        state = {"calls": 0}

        def gas(payload):
            state["calls"] += 1
            if state["calls"] == 1:
                raise RuntimeError("Read timed out")
            return _JsonRes({"results": [
                {"name": s["name"], "status": "sent", "recipient": "student"}
                for s in payload["students"]
            ]})

        h = _Harness(monkeypatch, names, gas=gas)
        lr.main()
        out = capsys.readouterr().out

        assert [len(uids) for uids in h.sent_uids] == [40, 1]
        flat = [uid for uids in h.sent_uids for uid in uids]
        assert len(flat) == len(set(flat)) == 41, "同じ宛先を2回送らない"
        assert "✅ 1名送信完了  ❌ 40名失敗" in out


class TestMainNotificationFailure:
    """通知が失敗しても main() は最後まで進む。落ちるとマーカーが残らず翌回に二重送信。"""

    def test_Slackが例外でも例外が漏れない(self, monkeypatch, capsys):
        def slack(url, payload):
            raise RuntimeError("slack down")

        h = _Harness(monkeypatch, ["山田太郎"],
                     gas=lambda payload: _JsonRes({"results": [
                         {"name": "山田太郎", "status": "failed", "recipient": "student",
                          "error": "UID形式が不正", "code": 400}]}),
                     slack=slack)
        lr.main()
        out = capsys.readouterr().out

        assert h.gas_posts, "送信自体は行われている"
        assert "slack down" in out

    def test_Slackに拒否されても例外が漏れない(self, monkeypatch, capsys):
        h = _Harness(monkeypatch, ["山田太郎"],
                     gas=lambda payload: _JsonRes(RuntimeError("boom")),
                     slack=lambda url, payload: _SlackRes(400, "invalid_blocks"))
        lr.main()
        out = capsys.readouterr().out

        assert "拒否されました" in out
        assert "✅ Slack通知送信" not in out
        assert h.failure_texts, "通知内容は組み立てられている"

    def test_LINE_IDが無い生徒はSlackに出て送信対象から外れる(self, monkeypatch, capsys):
        h = _Harness(monkeypatch, ["山田太郎", "名簿にいない子"])
        # 名簿から1人落とす
        roster = _roster(["山田太郎"])
        monkeypatch.setattr(lr.requests, "get",
                            lambda url, params=None, timeout=None: _JsonRes(roster))
        lr.main()
        out = capsys.readouterr().out

        assert h.sent_uids == [["U1"]]
        assert "LINE ID未登録" in "\n".join(h.failure_texts)
        assert "名簿にいない子" in out

    def test_名簿が空なら送らずに異常終了する(self, monkeypatch):
        """全員未送信のままマーカーを残さないため、ここだけは異常終了させる。"""
        h = _Harness(monkeypatch, ["山田太郎"])
        monkeypatch.setattr(lr.requests, "get",
                            lambda url, params=None, timeout=None: _JsonRes({"students": []}))
        with pytest.raises(SystemExit) as e:
            lr.main()

        assert e.value.code == 1
        assert h.gas_posts == [], "1件も送らない"
