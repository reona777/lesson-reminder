"""保護者リマインドの対象を名簿シートH列のチェックで決める部分のテスト。

以前はコード内の生徒名リストで管理していたため、希望者が増えるたびに
スクリプトを書き換えてpushする必要があった。設定をシートに移した分、
「チェックを読み違えて送られない／別人に送る」が起きないことを担保する。
"""

import pytest

import lesson_reminder as lr
import lesson_reminder_runner as runner


@pytest.fixture(autouse=True)
def _restore_targets():
    """本体側の対象集合を書き換えるので、テストごとに元へ戻す。"""
    original_runner = runner.PARENT_NOTIFY_TARGET_NAMES
    original_main = lr.PARENT_NOTIFY_TARGET_NAMES
    yield
    runner.PARENT_NOTIFY_TARGET_NAMES = original_runner
    lr.PARENT_NOTIFY_TARGET_NAMES = original_main


HEADER = ["生徒ＵＩＤ", "lineネーム", "生徒フルネーム", "",
          "保護者ＵＩＤ", "lineネーム", "生徒フルネーム", "保護者通知"]


def row(parent_uid="", student="", checked=""):
    return ["U_student", "ネーム", student, "", parent_uid, "ネーム", student, checked]


class TestIsParentNotifyChecked:
    @pytest.mark.parametrize("value", [True, "TRUE", "true", "True", 1, "1", "✓", "☑", "○"])
    def test_チェック済みと見なす値(self, value):
        assert runner.is_parent_notify_checked(value) is True

    @pytest.mark.parametrize("value", [False, "FALSE", "false", "", " ", 0, "0", None])
    def test_チェックなしと見なす値(self, value):
        assert runner.is_parent_notify_checked(value) is False


class TestParseParentRows:
    def test_チェックが入っている生徒だけ対象にする(self):
        rows = [
            HEADER,
            row("U_parent_a", "山田太郎", True),
            row("U_parent_b", "佐藤花子", False),
            row("U_parent_c", "鈴木一郎", ""),
        ]
        targets, parent_map = runner.parse_parent_rows(rows)
        assert targets == {lr.normalize("山田太郎")}
        assert parent_map == {lr.normalize("山田太郎"): "U_parent_a"}

    def test_チェックはあるが保護者UIDが空なら対象に残しmapには入れない(self):
        """本体側が「保護者LINE UID未登録」としてSlackに出すので、設定漏れが表に出る。"""
        rows = [HEADER, row("", "山田太郎", True)]
        targets, parent_map = runner.parse_parent_rows(rows)
        assert targets == {lr.normalize("山田太郎")}
        assert parent_map == {}

    def test_H列が省略された行でも落ちない(self):
        """末尾の空セルはSheets APIが省略して返すため、行の長さは揃っていない。"""
        rows = [HEADER, ["U_student", "ネーム", "山田太郎"], row("U_parent", "高橋美咲", True)]
        targets, parent_map = runner.parse_parent_rows(rows)
        assert targets == {lr.normalize("高橋美咲")}
        assert parent_map == {lr.normalize("高橋美咲"): "U_parent"}

    def test_生徒名が空の行は無視する(self):
        rows = [HEADER, row("U_parent", "", True)]
        targets, parent_map = runner.parse_parent_rows(rows)
        assert targets == set()
        assert parent_map == {}

    def test_表記ゆれを正規化して突き合わせる(self):
        """名簿とSalesforceで空白の有無が違うため、キーは normalize 済みで持つ。"""
        rows = [HEADER, row("U_parent", "山田　太郎", True)]
        targets, _ = runner.parse_parent_rows(rows)
        assert lr.normalize("山田太郎") in targets

    def test_ヘッダー行はチェックとして拾わない(self):
        targets, _ = runner.parse_parent_rows([HEADER])
        assert targets == set()


class TestApplyParentTargets:
    def test_本体側の対象集合も同時に入れ替わる(self):
        """本体の main() と build_parent_line_map() はこの集合を見て送信先を決める。"""
        runner.apply_parent_targets({lr.normalize("山田太郎")})
        assert lr.PARENT_NOTIFY_TARGET_NAMES == {lr.normalize("山田太郎")}
        assert runner.PARENT_NOTIFY_TARGET_NAMES == {lr.normalize("山田太郎")}


class TestFetchParentSettingsFromSheet:
    @pytest.fixture(autouse=True)
    def _sheet_id(self, monkeypatch):
        monkeypatch.setattr(runner, "PARENT_LINE_SPREADSHEET_ID", "dummy-sheet-id")

    def test_取得に失敗したらエラーを返し対象を空にする(self, monkeypatch):
        """誤って全員に送るより、送らずにSlackで気づける方を選ぶ。"""
        monkeypatch.setenv("CREDENTIALS_JSON", '{"client_email": "x@example.com"}')
        monkeypatch.setattr(
            runner, "fetch_sheet_rows_with_api",
            lambda creds: (_ for _ in ()).throw(RuntimeError("boom")))
        targets, parent_map, error = runner.fetch_parent_settings_from_sheet()
        assert targets == set()
        assert parent_map == {}
        assert "名簿シートを取得できません" in error

    def test_CREDENTIALS_JSON未設定ならエラーを返す(self, monkeypatch):
        monkeypatch.setenv("CREDENTIALS_JSON", "")
        targets, parent_map, error = runner.fetch_parent_settings_from_sheet()
        assert (targets, parent_map) == (set(), {})
        assert "CREDENTIALS_JSON" in error

    def test_シートIDが未設定ならエラーを返す(self, monkeypatch):
        monkeypatch.setattr(runner, "PARENT_LINE_SPREADSHEET_ID", "")
        targets, parent_map, error = runner.fetch_parent_settings_from_sheet()
        assert (targets, parent_map) == (set(), {})
        assert "PARENT_LINE_SPREADSHEET_ID" in error

    def test_読めたらチェック済みの生徒を返す(self, monkeypatch):
        monkeypatch.setenv("CREDENTIALS_JSON", '{"client_email": "x@example.com"}')
        monkeypatch.setattr(
            runner, "fetch_sheet_rows_with_api",
            lambda creds: [HEADER, row("U_parent", "山田太郎", True), row("U_x", "佐藤花子", False)])
        targets, parent_map, error = runner.fetch_parent_settings_from_sheet()
        assert targets == {lr.normalize("山田太郎")}
        assert parent_map == {lr.normalize("山田太郎"): "U_parent"}
        assert error == ""


class TestFetchIdsWithParentSheet:
    @pytest.fixture(autouse=True)
    def _sheet_id(self, monkeypatch):
        monkeypatch.setattr(runner, "PARENT_LINE_SPREADSHEET_ID", "dummy-sheet-id")

    def _stub_original_fetch_ids(self, monkeypatch, parent_map=None):
        monkeypatch.setattr(
            runner, "_original_fetch_ids",
            lambda: ({lr.normalize("山田太郎"): "U_student"}, parent_map or {}, {}))

    def test_シートの設定が本体に渡る(self, monkeypatch):
        self._stub_original_fetch_ids(monkeypatch)
        monkeypatch.setattr(
            runner, "fetch_parent_settings_from_sheet",
            lambda: ({lr.normalize("山田太郎")}, {lr.normalize("山田太郎"): "U_parent"}, ""))
        _line, parent_line_map, _slack = runner.fetch_ids_with_parent_sheet()
        assert parent_line_map == {lr.normalize("山田太郎"): "U_parent"}
        assert lr.PARENT_NOTIFY_TARGET_NAMES == {lr.normalize("山田太郎")}

    def test_シートを読めなければSlackに通知して保護者送信を止める(self, monkeypatch):
        self._stub_original_fetch_ids(monkeypatch)
        monkeypatch.setattr(
            runner, "fetch_parent_settings_from_sheet",
            lambda: (set(), {}, "名簿シートを取得できません: RuntimeError: boom"))
        notified = []
        monkeypatch.setattr(runner, "notify_parent_sheet_problem", notified.append)
        _line, parent_line_map, _slack = runner.fetch_ids_with_parent_sheet()
        assert parent_line_map == {}
        assert lr.PARENT_NOTIFY_TARGET_NAMES == set()
        assert len(notified) == 1

    def test_チェックが0件でもSlackに通知する(self, monkeypatch):
        """設定が消えたのか意図的に0にしたのか区別できないので、必ず知らせる。"""
        self._stub_original_fetch_ids(monkeypatch)
        monkeypatch.setattr(
            runner, "fetch_parent_settings_from_sheet", lambda: (set(), {}, ""))
        notified = []
        monkeypatch.setattr(runner, "notify_parent_sheet_problem", notified.append)
        runner.fetch_ids_with_parent_sheet()
        assert len(notified) == 1

    def test_GAS側が保護者UIDを返せばそちらを優先する(self, monkeypatch):
        """GASの名簿が正本。将来 parentId を返すようになったらシートより優先する。"""
        self._stub_original_fetch_ids(
            monkeypatch, parent_map={lr.normalize("山田太郎"): "U_from_gas"})
        monkeypatch.setattr(
            runner, "fetch_parent_settings_from_sheet",
            lambda: ({lr.normalize("山田太郎")}, {lr.normalize("山田太郎"): "U_from_sheet"}, ""))
        _line, parent_line_map, _slack = runner.fetch_ids_with_parent_sheet()
        assert parent_line_map == {lr.normalize("山田太郎"): "U_from_gas"}

    def test_シートIDが未設定なら保護者通知を使わない構成として黙って進む(self, monkeypatch):
        """保護者通知は任意機能。使っていない人に毎回Slack通知を飛ばさない。"""
        monkeypatch.setattr(runner, "PARENT_LINE_SPREADSHEET_ID", "")
        self._stub_original_fetch_ids(monkeypatch)
        notified = []
        monkeypatch.setattr(runner, "notify_parent_sheet_problem", notified.append)
        _line, parent_line_map, _slack = runner.fetch_ids_with_parent_sheet()
        assert parent_line_map == {}
        assert lr.PARENT_NOTIFY_TARGET_NAMES == set()
        assert notified == []
