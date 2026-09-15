"""LINE送信をまとめて投げる部分のテスト。

2026-09-10、26名＋保護者3名を1名1POSTで29回に分けて送ったところ、14件が
タイムアウト等で「送信できなかった」と記録された。GASの実行数ページを見ると
その14件も含めて doPost は1〜4秒で「完了」しており、Python側だけが応答を
受け取れていなかった。ただしGASはLINEの400応答も通信例外も握って正常終了できるため、
doPostの完了はGASが最後まで動いた証拠であって、LINEへの到達の証明ではない。

分かったこと2つ:
- 往復のたびにGoogle側のウェブアプリ層（/exec → googleusercontent の結果受け渡し）で
  詰まる。GASの処理自体は速い。往復回数がそのまま事故の確率になる。
- 同じ時刻に doGet が動いた実行があり、応答は {"error":"unauthorized"} だった。
  doGet は合言葉をURLクエリで見るのに送っているのはPOSTボディなので、送信処理から
  呼ばれればこうなる。ただし /exec にGETが戻る経路そのものは未解明で、
  「POSTが302の追従でGETに化ける」は仮説にすぎない。いずれにせよ results 無しとして
  握りつぶすとSlackのエラー欄が空になり原因が追えないので、中身は残す。

応答の形は本番GAS（doPost の data.students 分岐）に合わせている。
- 成功        : {name, status:'sent', recipient:'student'|'parent'}
- LINEが非200 : {name, status:'failed', recipient, error, code}
- GAS側の例外 : {name, status:'failed', error}      ← recipient も code も付かない
name はどの枝でも返る。Pythonが送る name も常に非空（生徒名を取れなかった授業は
送信対象に入らない）なので、name の無い応答はこの送信経路のものではない。
"""

import pytest

import lesson_reminder as lr


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


def _sent(name="山田太郎", recipient="student"):
    """本番GASが成功時に返す形。"""
    return {"name": name, "status": "sent", "recipient": recipient}


def _ok_echo(students, recipient="student"):
    """本物のGASと同じく、送った name をそのまま返す応答。"""
    return {"results": [_sent(s["name"], recipient) for s in students]}


def _fake_response(body):
    return type("R", (), {"json": lambda self: body})()


def _post_returning(body):
    return lambda *a, **k: _fake_response(body)


def _no_saved_result(monkeypatch):
    """GASに控えが残っていない状態。応答も引き取りも空振りする。

    応答から結果を読めないとき、送信処理は doGet?action=lessonSendResult で
    控えを取りに行く（2026-09-13）。ここが空振りしたときの挙動を見るテストでは、
    実際のGASを叩きに行かせないようこれを噛ませる。
    """
    monkeypatch.setattr(
        lr.requests, "get",
        lambda url, params=None, timeout=None: _fake_response({"requestId": None, "results": None}),
    )


class TestSendAll:
    def test_全員分が1回のPOSTにまとまる(self, monkeypatch):
        calls = []

        def fake_post(url, json=None, timeout=None):
            calls.append(json)
            return _fake_response({"results": [
                _sent(s["name"], "parent" if s["lineUserId"] == "U2" else "student")
                for s in json["students"]
            ]})

        monkeypatch.setattr(lr.requests, "post", fake_post)
        targets = [
            (_lesson("生徒A"), "本人", "U1"),
            (_lesson("生徒A"), "保護者", "U2"),
            (_lesson("生徒B"), "本人", "U3"),
        ]
        out = lr.send_all(targets)

        assert len(calls) == 1
        assert [s["lineUserId"] for s in calls[0]["students"]] == ["U1", "U2", "U3"]
        assert [ok for ok, _, _ in out] == [True, True, True]

    def test_結果は入力と同じ順で割り当てられる(self, monkeypatch):
        body = {"results": [
            _sent("A"),
            {"name": "B", "status": "failed", "recipient": "student",
             "error": "ユーザーにブロックされています", "code": 403},
            _sent("C"),
        ]}
        monkeypatch.setattr(lr.requests, "post", _post_returning(body))
        targets = [(_lesson("A"), "本人", "U1"), (_lesson("B"), "本人", "U2"), (_lesson("C"), "本人", "U3")]
        out = lr.send_all(targets)

        assert [ok for ok, _, _ in out] == [True, False, True]
        assert out[1][1] == "ユーザーにブロックされています"

    def test_unauthorizedが返っても理由が空にならない(self, monkeypatch):
        """doGetに化けた応答で、控えも引き取れなかった場合。エラー欄が空欄になってはいけない。"""
        monkeypatch.setattr(lr.requests, "post", _post_returning({"error": "unauthorized"}))
        _no_saved_result(monkeypatch)
        out = lr.send_all([(_lesson(), "本人", "U1")])

        ok, err, confirmed = out[0]
        assert ok is False
        assert err != ""
        assert "unauthorized" in err
        assert confirmed is False, "GASの返事ではないので未達とは断定できない"

    def test_応答が取れなくても未達と断定しない(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("Read timed out. (read timeout=120)")

        monkeypatch.setattr(lr.requests, "post", boom)
        _no_saved_result(monkeypatch)
        out = lr.send_all([(_lesson(), "本人", "U1"), (_lesson(), "保護者", "U2")])

        assert len(out) == 2
        for ok, err, confirmed in out:
            assert ok is False
            assert "Read timed out" in err
            assert confirmed is False

    def test_LINEが断ったときだけ未達が確定する(self, monkeypatch):
        body = {"results": [{"name": "山田太郎", "status": "failed", "recipient": "student",
                             "error": "UID形式が不正", "code": 400}]}
        monkeypatch.setattr(lr.requests, "post", _post_returning(body))
        ok, err, confirmed = lr.send_all([(_lesson(), "本人", "U1")])[0]

        assert (ok, err, confirmed) == (False, "UID形式が不正", True)

    def test_結果の数が足りなければそのchunk全体を結果不明にする(self, monkeypatch):
        """1件目だけ成功として扱うと、どの結果が誰のものか分からないまま通知してしまう。"""
        monkeypatch.setattr(lr.requests, "post", _post_returning({"results": [_sent()]}))
        out = lr.send_all([(_lesson(), "本人", "U1"), (_lesson(), "本人", "U2")])

        assert [ok for ok, _, _ in out] == [False, False]
        assert all(c is False for _, _, c in out)
        assert "件数が合いません" in out[0][1]

    def test_多すぎるときは分割して送る(self, monkeypatch):
        calls = []

        def fake_post(url, json=None, timeout=None):
            calls.append(len(json["students"]))
            return _fake_response(_ok_echo(json["students"]))

        monkeypatch.setattr(lr.requests, "post", fake_post)
        monkeypatch.setattr(lr, "BATCH_SIZE", 10)
        targets = [(_lesson(), "本人", f"U{i}") for i in range(25)]
        out = lr.send_all(targets)

        assert calls == [10, 10, 5]
        assert len(out) == 25
        assert all(ok for ok, _, _ in out)

    def test_送信対象が無くても1回だけ投げる(self, monkeypatch):
        """0件の日に何も投げないと、GAS側に控えが残らない。

        受け取る側（GASの13時台の見張り）は「今日の控えが無い＝リマインドが届いていない」
        で判定するので、授業が0件だった日と止まった日を見分けられなくなり、毎回誤検知する。
        GASは students が空配列なら results も空で返し、LINEは1通も送らない。
        """
        calls = []

        def fake_post(url, json=None, timeout=None):
            calls.append(json)
            return _fake_response({"results": []})

        monkeypatch.setattr(lr.requests, "post", fake_post)

        assert lr.send_all([]) == []
        assert len(calls) == 1
        assert calls[0]["students"] == []
        assert calls[0]["requestId"], "控えのキーが無いと記録が残らない"

    def test_合言葉と本文が載る(self, monkeypatch):
        calls = []

        def fake_post(url, json=None, timeout=None):
            calls.append(json)
            return _fake_response(_ok_echo(json["students"]))

        monkeypatch.setattr(lr.requests, "post", fake_post)
        lr.send_all([(_lesson("山田太郎"), "本人", "U1")])

        sent = calls[0]
        assert sent["token"] == lr.ROSTER_TOKEN
        assert sent["students"][0]["name"] == "山田太郎"
        assert "明日の授業の詳細です。" in sent["students"][0]["message"]

    def test_同じchunkを投げ直さない(self, monkeypatch):
        """GASは受け取った時点で最後まで送るので、投げ直すと二重送信になる。"""
        calls = []

        def boom(url, json=None, timeout=None):
            calls.append([s["lineUserId"] for s in json["students"]])
            raise RuntimeError("Read timed out")

        monkeypatch.setattr(lr.requests, "post", boom)
        _no_saved_result(monkeypatch)
        lr.send_all([(_lesson(), "本人", "U1")])

        assert calls == [["U1"]], "同じ宛先へ2回POSTしてはいけない"


class TestUndeliveredIsNarrow:
    """「GASがfailedを返した＝未達確定」は成立しない（2026-09-10のCodex監査）。

    GASの safeLinePush は UrlFetchApp の例外を捕まえて makeErrorResponse を返し、
    その getResponseCode() は 0。doPost はこれを status:failed / code:0 に変換する。
    LINEが受理した直後に応答を失っただけでも failed になるので、これを未達に混ぜると
    届いている生徒に「手動で送信してください」と言ってしまう。
    """

    def _send(self, monkeypatch, res):
        res = {"name": "山田太郎", "recipient": "student", **res}
        monkeypatch.setattr(lr.requests, "post", _post_returning({"results": [res]}))
        return lr.send_all([(_lesson(), "本人", "U1")])[0]

    def test_LINEが断ったときは未達が確定する(self, monkeypatch):
        for code in (400, 401, 403, 404, 429):
            ok, err, confirmed = self._send(monkeypatch, {
                "status": "failed", "error": "ユーザーにブロックされています", "code": code,
            })
            assert (ok, confirmed) == (False, True), f"code={code}"
            assert err == "ユーザーにブロックされています"

    def test_通信例外のcode0は未達と断定しない(self, monkeypatch):
        ok, err, confirmed = self._send(monkeypatch, {
            "status": "failed", "error": "エラー(0): Address unavailable", "code": 0,
        })
        assert ok is False
        assert confirmed is False, "LINEが受理した後に応答を失っただけかもしれない"
        assert "Address unavailable" in err

    def test_LINE側の5xxは未達と断定しない(self, monkeypatch):
        ok, _err, confirmed = self._send(monkeypatch, {
            "status": "failed", "error": "LINE APIサーバーエラー", "code": 500,
        })
        assert (ok, confirmed) == (False, False)

    def test_codeが無い失敗は未達と断定しない(self, monkeypatch):
        """doPost の catch 分岐は code を付けずに返す。"""
        ok, _err, confirmed = self._send(monkeypatch, {
            "status": "failed", "error": "TypeError: cannot read property",
        })
        assert (ok, confirmed) == (False, False)

    def test_拒否codeでもrecipientが無ければ未達と断定しない(self, monkeypatch):
        """LINEが非200を返した枝は必ず recipient を付ける（付かないのは catch 分岐）。

        recipient が無いのに拒否codeだけ付いた応答は契約の外なので、
        未達と言い切らずに人へ返す。GASを直したときに壊れるのはここ。
        """
        monkeypatch.setattr(lr.requests, "post", _post_returning({"results": [
            {"name": "山田太郎", "status": "failed", "error": "UID形式が不正", "code": 400},
        ]}))
        ok, err, confirmed = lr.send_all([(_lesson(), "本人", "U1")])[0]
        assert ok is False
        assert confirmed is False, "契約外の応答で未達と言い切ってはいけない"
        assert err == "UID形式が不正"

    def test_未知のstatusは未達と断定しない(self, monkeypatch):
        ok, _err, confirmed = self._send(monkeypatch, {"status": "queued"})
        assert (ok, confirmed) == (False, False)

    @pytest.mark.parametrize("status", ["queued", "pending", "", "SENT", "Failed"])
    def test_未知のstatusはcodeが拒否コードでも未達と断定しない(self, monkeypatch, status):
        """codeだけで決めると status:'queued' + code:400 が未達確定になってしまう。

        READMEの契約は「未知のstatusは結果を確認できなかった」。未達と言い切れるのは
        GASが failed を返し、かつLINEがHTTPで断ったと分かるコードが付いているときだけ。
        """
        ok, _err, confirmed = self._send(monkeypatch, {
            "status": status, "error": "UID形式が不正", "code": 400,
        })
        assert ok is False
        assert confirmed is False, f"status={status!r} で未達と言い切ってはいけない"

    def test_statusが無ければ未達と断定しない(self, monkeypatch):
        ok, _err, confirmed = self._send(monkeypatch, {"error": "UID形式が不正", "code": 400})
        assert (ok, confirmed) == (False, False)

    @pytest.mark.parametrize("code", ["400", 400.0, True, None])
    def test_整数でないcodeは未達と断定しない(self, monkeypatch, code):
        ok, _err, confirmed = self._send(monkeypatch, {
            "status": "failed", "error": "UID形式が不正", "code": code,
        })
        assert (ok, confirmed) == (False, False)

    def test_sentは成功として扱う(self, monkeypatch):
        ok, err, confirmed = self._send(monkeypatch, {"status": "sent"})
        assert (ok, err, confirmed) == (True, "", True)


class TestResultAlignment:
    """resultsの並びがずれても本文の宛先は狂わないが、成功／失敗の割り当てが入れ替わる。

    そのまま通知すると、届いている生徒に手動再送をかけることになる。
    """

    def _send_two(self, monkeypatch, results):
        monkeypatch.setattr(lr.requests, "post", _post_returning({"results": results}))
        return lr.send_all([
            (_lesson("Aさん"), "本人", "U1"),
            (_lesson("Bさん"), "本人", "U2"),
        ])

    def test_名前が入れ替わっていたら両方を結果不明にする(self, monkeypatch):
        out = self._send_two(monkeypatch, [
            {"name": "Bさん", "status": "failed", "recipient": "student", "error": "x", "code": 400},
            _sent("Aさん"),
        ])
        assert [ok for ok, _, _ in out] == [False, False]
        assert all(c is False for _, _, c in out)
        assert "送信順と違います" in out[0][1]

    def test_件数が合わなければ結果不明にする(self, monkeypatch):
        out = self._send_two(monkeypatch, [_sent("Aさん")])
        assert [ok for ok, _, _ in out] == [False, False]
        assert "件数が合いません" in out[0][1]

    def test_宛先の種別が食い違う分だけを結果不明にする(self, monkeypatch):
        """recipientの不一致はchunk全体を巻き込まない。

        GASは宛先UIDがlineシートE列にあるかで生徒/保護者を決めるので、同じUIDが
        生徒欄と保護者欄の両方に入っていると、並びが正しくてもここが食い違う。
        これでchunk全体を落とすと、本番29件が丸ごと「確認できませんでした」になる。
        """
        monkeypatch.setattr(lr.requests, "post", _post_returning({"results": [
            _sent("山田太郎", "parent"),
            _sent("山田太郎", "parent"),
        ]}))
        out = lr.send_all([
            (_lesson(), "本人", "U1"),
            (_lesson(), "保護者", "U2"),
        ])
        assert [ok for ok, _, _ in out] == [False, True], "巻き添えにしない"
        assert "宛先の種別が合いません" in out[0][1]
        assert out[0][2] is False

    def test_正しい宛先の種別なら通る(self, monkeypatch):
        """本番の形。生徒はstudent、保護者はparentで返る。"""
        monkeypatch.setattr(lr.requests, "post", _post_returning({"results": [
            _sent("山田太郎", "student"),
            _sent("山田太郎", "parent"),
        ]}))
        out = lr.send_all([
            (_lesson(), "本人", "U1"),
            (_lesson(), "保護者", "U2"),
        ])
        assert [ok for ok, _, _ in out] == [True, True]

    @pytest.mark.parametrize("broken", [
        {"status": "sent", "recipient": "student"},
        {"name": None, "status": "sent", "recipient": "student"},
        {"name": "", "status": "sent", "recipient": "student"},
    ])
    def test_nameが無い応答はchunk全体を結果不明にする(self, monkeypatch, broken):
        """本番GASは成功・失敗・catchのどの枝でも name を返す。

        Pythonが送る name も常に非空なので、name が無い応答はこの送信経路のものではない。
        並びを突き合わせられない以上、成功と信じるのではなく人に判断を返す。
        """
        out = self._send_two(monkeypatch, [dict(broken), dict(broken)])
        assert [ok for ok, _, _ in out] == [False, False]
        assert all(c is False for _, _, c in out)
        assert "生徒名" in out[0][1]

    def test_辞書でない結果はchunk全体を結果不明にする(self, monkeypatch):
        out = self._send_two(monkeypatch, ["sent", "sent"])
        assert [ok for ok, _, _ in out] == [False, False]
        assert "想定と違います" in out[0][1]

    def test_成功なのにrecipientが無ければ結果不明にする(self, monkeypatch):
        """本番GASは status:'sent' に必ず recipient を付ける（catch分岐は failed）。

        付いていない成功はこの送信経路の応答ではないので、届いたことにしない。
        """
        monkeypatch.setattr(lr.requests, "post", _post_returning({"results": [
            {"name": "山田太郎", "status": "sent"},
        ]}))
        ok, err, confirmed = lr.send_all([(_lesson(), "本人", "U1")])[0]
        assert (ok, confirmed) == (False, False)
        assert "宛先の種別" in err

    def test_catch分岐のrecipient欠落は失敗として扱える(self, monkeypatch):
        """GASの catch は {name, status:'failed', error} だけを返す。

        recipient が無いこと自体は想定内なので、理由を保ったまま結果不明にする。
        """
        monkeypatch.setattr(lr.requests, "post", _post_returning({"results": [
            {"name": "山田太郎", "status": "failed", "error": "TypeError: x"},
        ]}))
        ok, err, confirmed = lr.send_all([(_lesson(), "本人", "U1")])[0]
        assert (ok, confirmed) == (False, False)
        assert err == "TypeError: x"
