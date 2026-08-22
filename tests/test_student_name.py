"""授業名から生徒名を取り出す部分のテスト。

2026-08-12 に `8/5分[生徒名]…` の開き括弧が抜けた授業名が作られ、生徒名が取れず
リマインドから無言で消えた。届かないこと自体より、誰も気づけないことが問題だった。
その再発防止。
"""

import lesson_reminder as lr


class TestParseStudentName:
    def test_正しい形から生徒名を取る(self):
        assert lr.parse_student_name("[山田太郎]標準コースS(数):指導枠") == ("山田太郎", False)

    def test_振替元の日付が前に付いていても取れる(self):
        assert lr.parse_student_name("8/3分[山田太郎]標準コースS(数):指導枠") == ("山田太郎", False)

    def test_年付きの日付が前に付いていても取れる(self):
        assert lr.parse_student_name("未定2025/12/17分[鈴木一郎]標準コースS(英国):指導枠") == (
            "鈴木一郎",
            False,
        )

    def test_全角スペース入りでも取れる(self):
        assert lr.parse_student_name("3/27分　[高橋美咲]標準コースL(日):指導枠") == ("高橋美咲", False)

    def test_開き括弧が抜けていても推測として取る(self):
        """本件の再現。無言で落とさず、推測フラグ付きで拾う。"""
        assert lr.parse_student_name("8/5分山田太郎]標準コースS(英):指導枠") == ("山田太郎", True)

    def test_全角の日付プレフィックスでも推測できる(self):
        assert lr.parse_student_name("８／５分山田太郎]標準コースS(英):指導枠") == ("山田太郎", True)

    def test_日付プレフィックスなしで開き括弧が抜けた場合も推測する(self):
        assert lr.parse_student_name("山田太郎]標準コースS(英):指導枠") == ("山田太郎", True)

    def test_生徒のいない枠は生徒名なしになる(self):
        assert lr.parse_student_name("【社内】") == ("", False)

    def test_括弧がまったく無ければ生徒名なし(self):
        assert lr.parse_student_name("標準コースS(英):指導枠") == ("", False)

    def test_空文字でも落ちない(self):
        assert lr.parse_student_name("") == ("", False)
        assert lr.parse_student_name(None) == ("", False)

    def test_閉じ括弧の前が空なら生徒名なし(self):
        assert lr.parse_student_name("8/5分]標準コースS(英):指導枠") == ("", False)


class TestResolveStudentName:
    def test_授業名から取れるなら授業予定の生徒名は使わない(self):
        """LINE名簿は授業名と同じ表記なので、授業名を優先する（渡辺翔太／渡邊 翔太）。"""
        assert lr.resolve_student_name("[渡辺翔太]標準コースL(英数):指導枠", "渡邊 翔太") == (
            "渡辺翔太",
            False,
        )

    def test_括弧ごと消えていれば授業予定の生徒名で補う(self):
        assert lr.resolve_student_name("8/5分山田太郎標準コースS(英):指導枠", "山田 太郎") == (
            "山田 太郎",
            True,
        )

    def test_閉じ括弧が残っていれば授業名から推測する(self):
        assert lr.resolve_student_name("8/5分山田太郎]標準コースS(英):指導枠", "山田 太郎") == (
            "山田太郎",
            True,
        )

    def test_どちらも無ければ生徒名なし(self):
        assert lr.resolve_student_name("【社内】", "") == ("", False)

    def test_補完名の前後の空白は落とす(self):
        assert lr.resolve_student_name("標準コースS(英):指導枠", "  山田 太郎 ") == ("山田 太郎", True)


class TestFetchStudentNamesByLesson:
    class _FakeSf:
        def __init__(self, records=None, error=None):
            self.records = records or []
            self.error = error
            self.last_query = ""

        def query_all(self, q):
            self.last_query = q
            if self.error:
                raise self.error
            return {"records": self.records}

    def test_授業Idと生徒名の辞書になる(self):
        sf = self._FakeSf([
            {"MANAERP__Lesson__c": "a1c001", "MANAERP__Student_Name__c": "山田 太郎"},
            {"MANAERP__Lesson__c": "a1c002", "MANAERP__Student_Name__c": "渡邊 翔太"},
        ])
        assert lr.fetch_student_names_by_lesson(sf, "2026-08-12") == {
            "a1c001": "山田 太郎",
            "a1c002": "渡邊 翔太",
        }
        assert "2026-08-12" in sf.last_query

    def test_生徒名が空の行は入れない(self):
        sf = self._FakeSf([
            {"MANAERP__Lesson__c": "a1c001", "MANAERP__Student_Name__c": ""},
            {"MANAERP__Lesson__c": "a1c002", "MANAERP__Student_Name__c": None},
        ])
        assert lr.fetch_student_names_by_lesson(sf, "2026-08-12") == {}

    def test_取得に失敗しても空辞書を返して続行する(self):
        """補完は主経路ではないので、落とさず従来どおりの動作にする。"""
        sf = self._FakeSf(error=RuntimeError("SOQL error"))
        assert lr.fetch_student_names_by_lesson(sf, "2026-08-12") == {}


class TestSplitUnresolvedGuesses:
    def test_推測名が名簿に無ければ送信対象から外す(self):
        students = [
            {"生徒氏名": "山田太郎", "lineUserId": "", "_name_guessed": True},
            {"生徒氏名": "高橋美咲", "lineUserId": "U123", "_name_guessed": False},
        ]
        resolved, unresolved = lr.split_unresolved_guesses(students)
        assert [s["生徒氏名"] for s in resolved] == ["高橋美咲"]
        assert [s["生徒氏名"] for s in unresolved] == ["山田太郎"]

    def test_推測名でも名簿に一致すれば送信対象に残す(self):
        students = [{"生徒氏名": "山田太郎", "lineUserId": "U999", "_name_guessed": True}]
        resolved, unresolved = lr.split_unresolved_guesses(students)
        assert len(resolved) == 1
        assert unresolved == []

    def test_推測でない生徒はLINE_IDが無くても残す(self):
        """LINE ID未登録は従来どおり notify_slack_no_id 側で扱う。"""
        students = [{"生徒氏名": "新人生徒", "lineUserId": "", "_name_guessed": False}]
        resolved, unresolved = lr.split_unresolved_guesses(students)
        assert len(resolved) == 1
        assert unresolved == []


class TestNotifySlackSendFailed:
    """LINE送信の失敗はログにしか出ていなかったので、Slackに出ることを担保する。"""

    def _lesson(self, name="山田太郎"):
        return {
            "生徒氏名": name,
            "開始時間": "17:00",
            "終了時間": "18:00",
            "担当": "佐藤 花子",
            "授業名": f"[{name}]標準コースS(英):指導枠",
            "コース名": f"[{name}]標準コースS(英):指導枠",
            "科目": "英",
            "lineUserId": "U123",
            "parentLineUserId": "",
        }

    def test_失敗が無ければ何も送らない(self, monkeypatch):
        posted = []
        monkeypatch.setattr(lr.requests, "post", lambda *a, **k: posted.append(k))
        lr.notify_slack_send_failed([])
        assert posted == []

    def test_生徒名とエラーと本文が入る(self, monkeypatch):
        posted = {}

        def fake_post(url, json=None, timeout=None):
            posted["url"] = url
            posted["json"] = json
            return type("R", (), {"ok": True})()

        monkeypatch.setattr(lr.requests, "post", fake_post)
        lr.notify_slack_send_failed([(self._lesson(), "本人", "Read timed out")])

        blocks = posted["json"]["blocks"]
        assert "1件" in blocks[0]["text"]["text"]
        body = blocks[1]["text"]["text"]
        assert "山田太郎（本人宛）" in body
        assert "Read timed out" in body
        assert "明日の授業の詳細です。" in body

    def test_保護者宛の失敗も区別して入る(self, monkeypatch):
        posted = {}
        monkeypatch.setattr(
            lr.requests, "post",
            lambda url, json=None, timeout=None: posted.update(json=json)
        )
        lr.notify_slack_send_failed([
            (self._lesson(), "本人", "err1"),
            (self._lesson(), "保護者", "err2"),
        ])
        texts = [b["text"]["text"] for b in posted["json"]["blocks"]]
        assert any("（本人宛）" in t for t in texts)
        assert any("（保護者宛）" in t for t in texts)

    def test_Slackへのpostが落ちても例外を投げない(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("slack down")

        monkeypatch.setattr(lr.requests, "post", boom)
        lr.notify_slack_send_failed([(self._lesson(), "本人", "err")])
