"""POSTの応答を受け取れなかったときに、GASへ結果を取りに行く部分のテスト。

2026-09-13の12:07、29件を1回のPOSTで投げたところ、GASは53秒かけて全員に送り終えたのに
応答が /exec へのGETに化けて {"error":"unauthorized"} が返り、29件すべてが
「⚠️ LINEの送信結果を確認できませんでした」として本文つきでSlackへ流れた。
9/9・9/10も同じ形で、件数が多くGASの処理が長引いた日に起きている。
届いているのに毎回これが出るので、本当の未達が埋もれる方が危なくなっていた。

対処として、GAS側が doPost の結果を requestId つきでスクリプトプロパティに控え、
doGet?action=lessonSendResult&requestId=... で引き取れるようにした。ここはその挙動を固定する。

引き取りで拾えなかったときは従来どおり「結果を確認できませんでした」に落とす。
控えが取れないこと自体は、GASに届いていない可能性を否定しないため。
"""

import pytest

import lesson_reminder as lr


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    """引き取りの間隔（3・10・30・60秒）を実際に待つとテストにならない。"""
    monkeypatch.setattr(lr.time, "sleep", lambda s: None)


def _lesson(name="山田太郎"):
    return {
        "生徒氏名": name,
        "開始時間": "17:00",
        "終了時間": "18:00",
        "担当": "中澤 健吾",
        "授業名": f"[{name}]標準コースS(英):指導枠",
        "コース名": f"[{name}]標準コースS(英):指導枠",
        "科目": "英",
        "lineUserId": "U111",
        "parentLineUserId": "U222",
    }


def _fake_response(body):
    return type("R", (), {"json": lambda self: body})()


class _FakeGas:
    """本番GASの代わり。POSTを受けたら結果を控え、GETは requestId が一致したときだけ返す。

    控える・返すの形は コード.js の saveBulkSendResult_ と doGet の
    action=lessonSendResult に合わせている。
    """

    def __init__(self, post_body=None, post_error=None, parent_uids=(), results_of=None):
        self.saved = {}
        self.post_calls = []
        self.get_calls = []
        self.post_body = post_body        # None なら本物と同じく {"results": ...} を返す
        self.post_error = post_error      # タイムアウト等を再現する
        self.parent_uids = set(parent_uids)
        self.results_of = results_of      # 結果の中身を差し替えたいテスト用

    def _results(self, students):
        if self.results_of:
            return self.results_of(students)
        return [
            {
                "name": s["name"],
                "status": "sent",
                "recipient": "parent" if s["lineUserId"] in self.parent_uids else "student",
            }
            for s in students
        ]

    def post(self, url, json=None, timeout=None):
        self.post_calls.append(json)
        results = self._results(json["students"])
        # GASは応答を返す前に控える。応答が化けても結果は残る、という順序をここでも守る。
        if json.get("requestId"):
            self.saved[json["requestId"]] = results
        if self.post_error:
            raise self.post_error
        return _fake_response(self.post_body if self.post_body is not None else {"results": results})

    def get(self, url, params=None, timeout=None):
        self.get_calls.append(params)
        rid = params.get("requestId")
        if rid in self.saved:
            return _fake_response({"requestId": rid, "results": self.saved[rid]})
        return _fake_response({"requestId": None, "results": None})

    def install(self, monkeypatch):
        monkeypatch.setattr(lr.requests, "post", self.post)
        monkeypatch.setattr(lr.requests, "get", self.get)
        return self


class TestFetchSavedResults:
    def test_unauthorizedでも控えを取りに行って成功と分かる(self, monkeypatch):
        """2026-09-13の再現。応答だけが化けていて、GASは送り終えている。"""
        gas = _FakeGas(post_body={"error": "unauthorized"}).install(monkeypatch)
        targets = [(_lesson("A"), "本人", "U1"), (_lesson("B"), "本人", "U2")]

        out = lr.send_all(targets)

        assert [ok for ok, _, _ in out] == [True, True]
        assert len(gas.get_calls) == 1, "1回目の引き取りで拾えるはず"
        assert gas.get_calls[0]["action"] == "lessonSendResult"

    def test_POSTがタイムアウトしても控えを取りに行く(self, monkeypatch):
        _FakeGas(post_error=RuntimeError("Read timed out. (read timeout=180)")).install(monkeypatch)

        out = lr.send_all([(_lesson("A"), "本人", "U1")])

        assert [ok for ok, _, _ in out] == [True]

    def test_保護者宛も控えから正しく割り当てられる(self, monkeypatch):
        gas = _FakeGas(post_body={"error": "unauthorized"}, parent_uids={"U2"}).install(monkeypatch)
        targets = [(_lesson("A"), "本人", "U1"), (_lesson("A"), "保護者", "U2")]

        out = lr.send_all(targets)

        assert [ok for ok, _, _ in out] == [True, True]
        assert gas.saved[gas.post_calls[0]["requestId"]][1]["recipient"] == "parent"

    def test_requestIdが違う控えは採用しない(self, monkeypatch):
        """前の実行の控えを今回の結果として読むと、届いていないものを届いたことにしてしまう。"""
        gas = _FakeGas(post_body={"error": "unauthorized"}).install(monkeypatch)

        def stale_get(url, params=None, timeout=None):
            gas.get_calls.append(params)
            return _fake_response({
                "requestId": "20260912120700-deadbeef-0",
                "results": [{"name": "A", "status": "sent", "recipient": "student"}],
            })

        monkeypatch.setattr(lr.requests, "get", stale_get)
        ok, err, confirmed = lr.send_all([(_lesson("A"), "本人", "U1")])[0]

        assert ok is False
        assert "unauthorized" in err
        assert confirmed is False

    def test_控えが無ければ従来どおり結果不明のまま(self, monkeypatch):
        gas = _FakeGas(post_body={"error": "unauthorized"}).install(monkeypatch)
        gas.saved.clear()  # POSTの直後に控えを消す＝GASが保存に失敗した状態

        def empty_get(url, params=None, timeout=None):
            gas.get_calls.append(params)
            return _fake_response({"requestId": None, "results": None})

        monkeypatch.setattr(lr.requests, "get", empty_get)
        ok, err, confirmed = lr.send_all([(_lesson("A"), "本人", "U1")])[0]

        assert ok is False
        assert "unauthorized" in err, "引き取れなかったときは元の応答を理由に残す"
        assert confirmed is False
        assert len(gas.get_calls) == len(lr.RESULT_RETRY_WAITS), "諦めるまで決めた回数だけ試す"

    def test_引き取りが例外でも落ちず次の間隔で試す(self, monkeypatch):
        gas = _FakeGas(post_body={"error": "unauthorized"}).install(monkeypatch)
        real_get = gas.get
        state = {"n": 0}

        def flaky_get(url, params=None, timeout=None):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("Connection aborted")
            return real_get(url, params=params, timeout=timeout)

        monkeypatch.setattr(lr.requests, "get", flaky_get)
        out = lr.send_all([(_lesson("A"), "本人", "U1")])

        assert [ok for ok, _, _ in out] == [True]
        assert state["n"] == 2

    def test_控えの並びが送信順と違えば結果不明にする(self, monkeypatch):
        """引き取った結果でも並びの検査は素通しさせない。"""
        _FakeGas(
            post_body={"error": "unauthorized"},
            results_of=lambda students: [
                {"name": "別人", "status": "sent", "recipient": "student"} for _ in students
            ],
        ).install(monkeypatch)

        ok, err, confirmed = lr.send_all([(_lesson("A"), "本人", "U1")])[0]

        assert ok is False
        assert "並び" in err
        assert confirmed is False

    def test_控えから未達が確定することもある(self, monkeypatch):
        _FakeGas(
            post_body={"error": "unauthorized"},
            results_of=lambda students: [
                {"name": s["name"], "status": "failed", "recipient": "student",
                 "error": "UID形式が不正", "code": 400}
                for s in students
            ],
        ).install(monkeypatch)

        ok, err, confirmed = lr.send_all([(_lesson("A"), "本人", "U1")])[0]

        assert (ok, err, confirmed) == (False, "UID形式が不正", True)


class TestRequestId:
    def test_POSTにrequestIdが載る(self, monkeypatch):
        gas = _FakeGas().install(monkeypatch)

        lr.send_all([(_lesson("A"), "本人", "U1")])

        assert gas.post_calls[0]["requestId"]

    def test_正常な応答なら引き取りに行かない(self, monkeypatch):
        gas = _FakeGas().install(monkeypatch)

        out = lr.send_all([(_lesson("A"), "本人", "U1")])

        assert [ok for ok, _, _ in out] == [True]
        assert gas.get_calls == [], "余計な往復を増やさない"

    def test_チャンクごとに別のrequestIdになる(self, monkeypatch):
        """同じidを使い回すと、後のチャンクが前のチャンクの控えを読んでしまう。"""
        gas = _FakeGas().install(monkeypatch)
        monkeypatch.setattr(lr, "BATCH_SIZE", 1)

        lr.send_all([(_lesson("A"), "本人", "U1"), (_lesson("B"), "本人", "U2")])

        ids = [c["requestId"] for c in gas.post_calls]
        assert len(ids) == 2
        assert ids[0] != ids[1]
