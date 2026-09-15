"""Slack通知（Incoming Webhook）まわりのテスト。

見ているのは3つ。

1. 50 blocksで分割したあと、各ページ単独で「何の通知か」「未達か結果不明か」が分かること。
   未達と結果不明が混ざったまま切ると、後続ページから二重送信の注意が消える。
2. section本文を3000字で切るときにコードフェンスを閉じること。開いたままだと
   以降の表示が崩れ、手動送信する本文を読み違える。
3. Incoming Webhookの成功はHTTP200かつ本文が `ok` のときだけ（Slack公式）。
   429は Retry-After で待つよう案内されるので、そこだけ有界に1回だけ待ち直す。
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
        "lineUserId": "U123",
        "parentLineUserId": "",
        "_start_dt": None,
    }


class _Res:
    """Incoming Webhookの応答。成功は200 + 本文 ok。"""

    def __init__(self, status_code=200, text="ok", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def _recorder(monkeypatch, res=None):
    """postされたページを順に記録する。戻り値は blocks のリストのリスト。"""
    pages = []

    def fake_post(url, json=None, timeout=None):
        pages.append(json.get("blocks", json))
        return res() if callable(res) else (res or _Res())

    monkeypatch.setattr(lr.requests, "post", fake_post)
    monkeypatch.setattr(lr.time, "sleep", lambda s: None)
    return pages


def _texts(page):
    return [b["text"]["text"] for b in page]


def _is_header(text):
    return text.startswith(("❌", "⚠️", "📅"))


class TestPageHeadings:
    """分割しても各ページに見出しが載ること。"""

    def test_未達と結果不明が混ざっても各ページに見出しが載る(self, monkeypatch):
        """後続ページに不明分だけが載ると、二重送信の注意が消えたまま届く。"""
        pages = _recorder(monkeypatch)
        failures = [(_lesson(f"未達{i}"), "本人", "ブロックされています", True) for i in range(30)]
        failures += [(_lesson(f"不明{i}"), "本人", "確認できず", False) for i in range(30)]
        lr.notify_slack_send_failed(failures)

        assert len(pages) >= 2, "50 blocksを超えるので分割されるはず"
        for page in pages:
            texts = _texts(page)
            assert len(page) <= lr.SLACK_MAX_BLOCKS
            assert _is_header(texts[0]), "各ページの先頭は見出し"
            assert any(not _is_header(t) for t in texts), "見出しだけのページを作らない"
            if any("不明" in t and not _is_header(t) for t in texts):
                assert any("二重送信" in t for t in texts), "不明分が載るページには必ず注意書きを添える"
            if any("未達" in t and not _is_header(t) for t in texts):
                assert any("送信できませんでした" in t for t in texts)

    def test_分割しても取りこぼしも重複もしない(self, monkeypatch):
        pages = _recorder(monkeypatch)
        failures = [(_lesson(f"未達{i}"), "本人", "ブロックされています", True) for i in range(30)]
        failures += [(_lesson(f"不明{i}"), "本人", "確認できず", False) for i in range(30)]
        lr.notify_slack_send_failed(failures)

        details = [t for page in pages for t in _texts(page) if not _is_header(t)]
        names = [f"未達{i}" for i in range(30)] + [f"不明{i}" for i in range(30)]
        assert len(details) == 60
        for name in names:
            assert sum(1 for t in details if f"*{name}（本人宛）*" in t) == 1

    def test_1グループが大きくても見出しを繰り返す(self, monkeypatch):
        pages = _recorder(monkeypatch)
        lr.notify_slack_send_failed([
            (_lesson(f"不明{i}"), "本人", "確認できず", False) for i in range(60)
        ])

        assert [len(p) for p in pages] == [50, 12], "見出し1＋詳細49 / 見出し1＋詳細11"
        for page in pages:
            texts = _texts(page)
            assert "確認できませんでした" in texts[0]
            assert "二重送信" in texts[0]

    def test_29件なら1回で収まる(self, monkeypatch):
        """本番の想定件数。見出し1＋詳細29＝30 blocks。"""
        pages = _recorder(monkeypatch)
        lr.notify_slack_send_failed([
            (_lesson(f"生徒{i}"), "本人", "確認できず", False) for i in range(29)
        ])
        assert [len(p) for p in pages] == [30]

    def test_ちょうど49件なら1回(self, monkeypatch):
        pages = _recorder(monkeypatch)
        lr.notify_slack_send_failed([
            (_lesson(f"生徒{i}"), "本人", "確認できず", False) for i in range(49)
        ])
        assert [len(p) for p in pages] == [50]

    def test_50件なら分割して見出しを再掲する(self, monkeypatch):
        pages = _recorder(monkeypatch)
        lr.notify_slack_send_failed([
            (_lesson(f"生徒{i}"), "本人", "確認できず", False) for i in range(50)
        ])
        assert [len(p) for p in pages] == [50, 2]
        assert "確認できませんでした" in _texts(pages[1])[0]

    def test_詳細が無いグループは見出しを出さない(self, monkeypatch):
        pages = _recorder(monkeypatch)
        lr.notify_slack_send_failed([(_lesson(), "本人", "確認できず", False)])
        texts = _texts(pages[0])
        assert not any("送信できませんでした" in t for t in texts)
        assert any("確認できませんでした" in t for t in texts)

    def test_失敗が無ければ何も送らない(self, monkeypatch):
        pages = _recorder(monkeypatch)
        lr.notify_slack_send_failed([])
        assert pages == []


class TestSlackSection:
    """3000字上限で切るときにコードフェンスを閉じる。"""

    def test_短い本文はそのまま(self):
        text = "*山田太郎*\n```明日の授業の詳細です。```"
        assert lr.slack_section(text)["text"]["text"] == text

    def test_長すぎる本文は3000字の手前で切る(self):
        block = lr.slack_section("あ" * 5000)
        assert len(block["text"]["text"]) < 3000

    def test_開いたままのコードフェンスを閉じる(self):
        text = "*生徒*\n```" + "あ" * 5000
        out = lr.slack_section(text)["text"]["text"]
        assert len(out) < 3000
        assert out.count("```") % 2 == 0, "開きっぱなしにしない"
        assert out.rstrip().endswith("```")

    def test_閉じたフェンスに余計な閉じを足さない(self):
        text = "```" + "あ" * 5000 + "```"
        out = lr.slack_section(text)["text"]["text"]
        assert out.count("```") == 2

    @pytest.mark.parametrize("offset", [0, 1, 2, 3])
    def test_フェンスの途中で切れても壊れない(self, offset):
        """切れ目が ``` の1文字目・2文字目に当たると中途半端なバッククォートが残る。"""
        head = "```" + "あ" * (lr.SLACK_MAX_TEXT - 3 - offset)
        text = head + "```" + "い" * 100
        out = lr.slack_section(text)["text"]["text"]
        assert len(out) < 3000
        assert out.count("```") % 2 == 0
        assert not out.rstrip("`").endswith("``"), "中途半端なバッククォートを残さない"

    def test_省略したことが分かる(self):
        out = lr.slack_section("あ" * 5000)["text"]["text"]
        assert "省略" in out


class TestWebhookResult:
    """Incoming Webhookの成功はHTTP200かつ本文 ok のときだけ。"""

    def _post(self, monkeypatch, res):
        _recorder(monkeypatch, res)
        lr.post_slack_groups([(lr.slack_section("⚠️ 見出し"), [lr.slack_section("本文")])], "テスト")

    def test_200とokなら成功と出す(self, monkeypatch, capsys):
        self._post(monkeypatch, _Res(200, "ok"))
        assert "✅" in capsys.readouterr().out

    def test_200でも本文がokでなければ成功と言わない(self, monkeypatch, capsys):
        self._post(monkeypatch, _Res(200, "invalid_payload"))
        out = capsys.readouterr().out
        assert "✅" not in out
        assert "invalid_payload" in out

    def test_400なら成功と言わない(self, monkeypatch, capsys):
        self._post(monkeypatch, _Res(400, "invalid_blocks"))
        out = capsys.readouterr().out
        assert "✅" not in out
        assert "invalid_blocks" in out

    @pytest.mark.parametrize("res", [
        type("NoStatus", (), {"text": "ok"})(),
        _Res(None, "ok"),
        _Res(200, None),
        _Res(200, ""),
    ])
    def test_応答の形が想定外なら成功と言わない(self, monkeypatch, capsys, res):
        """モックが status_code を持たないだけで成功扱いになると、拒否を見逃す。"""
        self._post(monkeypatch, res)
        assert "✅" not in capsys.readouterr().out

    def test_例外でも落ちず成功とも言わない(self, monkeypatch, capsys):
        def boom(*a, **k):
            raise RuntimeError("slack down")

        monkeypatch.setattr(lr.requests, "post", boom)
        monkeypatch.setattr(lr.time, "sleep", lambda s: None)
        lr.post_slack_groups([(lr.slack_section("⚠️ 見出し"), [lr.slack_section("本文")])], "テスト")
        out = capsys.readouterr().out
        assert "✅" not in out
        assert "slack down" in out

    @pytest.mark.parametrize("res", [_Res(500, "server_error"), _Res(503, "service_unavailable"),
                                     _Res(400, "invalid_blocks")])
    def test_429以外は投げ直さない(self, monkeypatch, res):
        """待てば通るのは429だけ。結果が分からない要求を投げ直すと二重に通知される。"""
        pages = _recorder(monkeypatch, res)
        lr.post_slack_groups([(lr.slack_section("⚠️ 見出し"), [lr.slack_section("本文")])], "テスト")
        assert len(pages) == 1

    def test_例外でも投げ直さない(self, monkeypatch):
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise RuntimeError("Read timed out")

        monkeypatch.setattr(lr.requests, "post", boom)
        monkeypatch.setattr(lr.time, "sleep", lambda s: None)
        lr.post_slack_groups([(lr.slack_section("⚠️ 見出し"), [lr.slack_section("本文")])], "テスト")
        assert calls == [1]

    def test_1ページ拒否されても残りは送る(self, monkeypatch):
        """400は待っても直らないが、他のページまで巻き添えにする理由もない。"""
        pages = _recorder(monkeypatch, _Res(400, "invalid_blocks"))
        lr.notify_slack_send_failed([
            (_lesson(f"生徒{i}"), "本人", "確認できず", False) for i in range(60)
        ])
        assert len(pages) == 2


class TestRateLimit:
    """429だけは Retry-After に従って1回だけ待ち直す（Slack公式）。"""

    def _send_two_pages(self, monkeypatch, responses):
        pages, waits = [], []
        queue = list(responses)

        def fake_post(url, json=None, timeout=None):
            pages.append(json["blocks"])
            return queue.pop(0) if queue else _Res()

        monkeypatch.setattr(lr.requests, "post", fake_post)
        monkeypatch.setattr(lr.time, "sleep", lambda s: waits.append(s))
        lr.notify_slack_send_failed([
            (_lesson(f"生徒{i}"), "本人", "確認できず", False) for i in range(60)
        ])
        return pages, waits

    def test_429なら指定秒待って1回だけ送り直す(self, monkeypatch):
        pages, waits = self._send_two_pages(
            monkeypatch, [_Res(429, "rate_limited", {"Retry-After": "3"}), _Res(), _Res()]
        )
        assert len(pages) == 3, "1ページ目を1回だけ送り直して、2ページ目も送る"
        assert pages[0] == pages[1], "同じページを送り直す"
        assert 3 in waits

    def test_待ち時間の上限を超えるRetry_Afterでは送り直さない(self, monkeypatch, capsys):
        pages, waits = self._send_two_pages(
            monkeypatch, [_Res(429, "rate_limited", {"Retry-After": "3600"})]
        )
        assert len(pages) == 1, "待てない以上、送り直しも残りページの送信もしない"
        assert all(w <= lr.SLACK_RETRY_AFTER_MAX_SEC for w in waits)
        assert "3600" in capsys.readouterr().out

    @pytest.mark.parametrize("header", [{}, {"Retry-After": ""}, {"Retry-After": "すぐ"}, {"Retry-After": "-5"}])
    def test_Retry_Afterが読めなければ送り直さない(self, monkeypatch, header):
        pages, _waits = self._send_two_pages(monkeypatch, [_Res(429, "rate_limited", header)])
        assert len(pages) == 1

    def test_送り直しても429なら残りページを送らない(self, monkeypatch):
        pages, waits = self._send_two_pages(monkeypatch, [
            _Res(429, "rate_limited", {"Retry-After": "1"}),
            _Res(429, "rate_limited", {"Retry-After": "1"}),
        ])
        assert len(pages) == 2, "2回投げたらそこで止める"
        assert waits.count(1) == 1, "待ち直しは1回だけ"

    def test_429で止めたことがログに残る(self, monkeypatch, capsys):
        self._send_two_pages(monkeypatch, [_Res(429, "rate_limited", {"Retry-After": "1"}),
                                           _Res(429, "rate_limited", {"Retry-After": "1"})])
        out = capsys.readouterr().out
        assert "レート制限" in out
        assert "送っていません" in out


class TestRateLimitAcrossCalls:
    """レート制限で止めた宛先は、通知関数をまたいでも止まったままにする。

    main() は未登録・保護者UID欠落・特定できない授業・送信失敗の4本を続けて呼ぶ。
    1呼び出しの中でしか止まらないと、Retry-Afterを無視して同じチャンネルへ投げ続け、
    締め出しが伸びて後続の通知がまとめて消える。
    """

    def _always_429(self, monkeypatch, retry_after="3600"):
        posted = []

        def fake_post(url, json=None, timeout=None):
            posted.append(url)
            return _Res(429, "rate_limited", {"Retry-After": retry_after})

        monkeypatch.setattr(lr, "SLACK_WEBHOOK", "https://hooks.slack.example/failed")
        monkeypatch.setattr(lr, "SLACK_TEACHER_WEBHOOK", "https://hooks.slack.example/teacher")
        monkeypatch.setattr(lr.requests, "post", fake_post)
        monkeypatch.setattr(lr.time, "sleep", lambda s: None)
        return posted

    def test_止めた宛先には別の通知関数からも送らない(self, monkeypatch):
        posted = self._always_429(monkeypatch)
        students = [_lesson("山田太郎")]

        lr.notify_slack_no_id(students)
        lr.notify_slack_parent_uid_missing(students)
        lr.notify_slack_unparsed(students)
        lr.notify_slack_send_failed([(_lesson(), "本人", "確認できず", False)])

        assert len(posted) == 1, "待てない指定を受けたあとに投げ直してはいけない"

    def test_止めた宛先には同じ通知関数からも送らない(self, monkeypatch):
        posted = self._always_429(monkeypatch)
        lr.notify_slack_no_id([_lesson("山田太郎")])
        lr.notify_slack_no_id([_lesson("高橋美咲")])
        assert len(posted) == 1

    def test_再試行して429のままでも以降を止める(self, monkeypatch):
        """Retry-Afterに従って1回だけ投げ直したあと、上限を超えて投げ続けない。"""
        posted = self._always_429(monkeypatch, retry_after="1")
        lr.notify_slack_no_id([_lesson("山田太郎")])
        lr.notify_slack_no_id([_lesson("高橋美咲")])
        assert len(posted) == 2, "1通目の再試行1回だけ"

    def test_別の宛先は止めない(self, monkeypatch):
        """止めるのは締め出された宛先だけ。講師リマインドは別チャンネル。"""
        posted = self._always_429(monkeypatch)
        lr.notify_slack_no_id([_lesson("山田太郎")])
        lr.notify_slack_teacher_remind([_lesson("山田太郎")], {})

        assert posted == ["https://hooks.slack.example/failed",
                          "https://hooks.slack.example/teacher"]

    def test_送り直して通れば止めない(self, monkeypatch):
        posted = []
        queue = [_Res(429, "rate_limited", {"Retry-After": "1"})]

        def fake_post(url, json=None, timeout=None):
            posted.append(url)
            return queue.pop(0) if queue else _Res()

        monkeypatch.setattr(lr.requests, "post", fake_post)
        monkeypatch.setattr(lr.time, "sleep", lambda s: None)
        lr.notify_slack_no_id([_lesson("山田太郎")])
        lr.notify_slack_no_id([_lesson("高橋美咲")])

        assert len(posted) == 3, "再試行1回＋2本目の通知"

    def test_未送信をログに残しURLは出さない(self, monkeypatch, capsys):
        self._always_429(monkeypatch)
        lr.notify_slack_no_id([_lesson("山田太郎")])
        lr.notify_slack_unparsed([_lesson("山田太郎")])
        out = capsys.readouterr().out

        assert "送っていません" in out
        assert "生徒を特定できない授業" in out, "どの通知が送れていないかは分かるようにする"
        assert "hooks.slack.example" not in out, "宛先URLは秘密なのでログに出さない"

    def test_状態がテスト間に持ち越されない(self, monkeypatch):
        """直前のテストで止めた宛先が残っていると、この通知は送られない。"""
        posted = []
        monkeypatch.setattr(
            lr.requests, "post",
            lambda url, json=None, timeout=None: (posted.append(url), _Res())[1])
        monkeypatch.setattr(lr.time, "sleep", lambda s: None)
        lr.notify_slack_no_id([_lesson("山田太郎")])
        assert len(posted) == 1


class TestOtherNotifications:
    """LINE ID未登録・保護者UID未登録・生徒を特定できない授業の3本。

    どれも「気づかせる」ためだけの通知なので、空でも大量でも拒否されても落ちてはいけない。
    """

    NOTIFIERS = ["notify_slack_no_id", "notify_slack_parent_uid_missing", "notify_slack_unparsed"]

    @pytest.mark.parametrize("name", NOTIFIERS)
    def test_空なら何も送らない(self, monkeypatch, name):
        pages = _recorder(monkeypatch)
        getattr(lr, name)([])
        assert pages == []

    @pytest.mark.parametrize("name", NOTIFIERS)
    def test_件数と見出しと本文が載る(self, monkeypatch, name):
        pages = _recorder(monkeypatch)
        getattr(lr, name)([_lesson("山田太郎"), _lesson("高橋美咲")])

        texts = _texts(pages[0])
        assert "2" in texts[0], "件数を見出しに出す"
        assert _is_header(texts[0])
        assert "山田太郎" in "\n".join(texts[1:])
        assert "高橋美咲" in "\n".join(texts[1:])

    def test_LINE_ID未登録は送信本文をそのまま載せる(self, monkeypatch):
        pages = _recorder(monkeypatch)
        lr.notify_slack_no_id([_lesson("山田太郎")])
        body = _texts(pages[0])[1]
        assert "*山田太郎*" in body
        assert "明日の授業の詳細です。" in body

    def test_保護者UID未登録は保護者宛と分かる(self, monkeypatch):
        pages = _recorder(monkeypatch)
        lr.notify_slack_parent_uid_missing([_lesson("山田太郎")])
        texts = _texts(pages[0])
        assert "保護者" in texts[0]
        assert "（保護者宛）" in texts[1]

    def test_特定できない授業は授業名と時間を載せる(self, monkeypatch):
        pages = _recorder(monkeypatch)
        lesson = _lesson("")
        lesson["授業名"] = "8/5分山田太郎]標準コースS(英):指導枠"
        lr.notify_slack_unparsed([lesson])
        body = _texts(pages[0])[1]
        assert "8/5分山田太郎]" in body
        assert "17:00" in body

    def test_推測できた名前は推測と分かるように出す(self, monkeypatch):
        pages = _recorder(monkeypatch)
        lr.notify_slack_unparsed([_lesson("山田太郎")])
        body = _texts(pages[0])[1]
        assert "推測" in body

    @pytest.mark.parametrize("name", NOTIFIERS)
    def test_50件を超えても分割して見出しを再掲する(self, monkeypatch, name):
        pages = _recorder(monkeypatch)
        getattr(lr, name)([_lesson(f"生徒{i}") for i in range(50)])

        assert [len(p) for p in pages] == [50, 2]
        for page in pages:
            texts = _texts(page)
            assert _is_header(texts[0])
            assert len(page) <= lr.SLACK_MAX_BLOCKS
        details = [t for p in pages for t in _texts(p) if not _is_header(t)]
        assert len(details) == 50

    @pytest.mark.parametrize("name", NOTIFIERS)
    def test_ちょうど49件なら1回(self, monkeypatch, name):
        pages = _recorder(monkeypatch)
        getattr(lr, name)([_lesson(f"生徒{i}") for i in range(49)])
        assert [len(p) for p in pages] == [50]

    @pytest.mark.parametrize("name", NOTIFIERS)
    def test_Slackに拒否されても落ちない(self, monkeypatch, capsys, name):
        _recorder(monkeypatch, _Res(400, "invalid_blocks"))
        getattr(lr, name)([_lesson("山田太郎")])
        assert "✅" not in capsys.readouterr().out

    @pytest.mark.parametrize("name", NOTIFIERS)
    def test_Slackへのpostが落ちても例外を投げない(self, monkeypatch, name):
        def boom(*a, **k):
            raise RuntimeError("slack down")

        monkeypatch.setattr(lr.requests, "post", boom)
        monkeypatch.setattr(lr.time, "sleep", lambda s: None)
        getattr(lr, name)([_lesson("山田太郎")])


class TestTeacherRemind:
    def test_成功条件を満たさなければ成功と言わない(self, monkeypatch, capsys):
        _recorder(monkeypatch, _Res(500, "server_error"))
        lr.notify_slack_teacher_remind([_lesson("山田太郎")], {})
        out = capsys.readouterr().out
        assert "✅" not in out

    def test_例外でも落ちない(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("slack down")

        monkeypatch.setattr(lr.requests, "post", boom)
        monkeypatch.setattr(lr.time, "sleep", lambda s: None)
        lr.notify_slack_teacher_remind([_lesson("山田太郎")], {})

    def test_担当ごとにまとめて送る(self, monkeypatch):
        posted = []

        def fake_post(url, json=None, timeout=None):
            posted.append(json)
            return _Res()

        monkeypatch.setattr(lr.requests, "post", fake_post)
        monkeypatch.setattr(lr.time, "sleep", lambda s: None)
        lr.notify_slack_teacher_remind([_lesson("山田太郎"), _lesson("高橋美咲")], {})

        assert len(posted) == 1
        assert "山田太郎" in posted[0]["text"]
        assert "高橋美咲" in posted[0]["text"]
