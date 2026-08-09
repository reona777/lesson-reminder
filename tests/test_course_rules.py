"""コース名の判定表・体験回・除外キーワードの挙動を確認する。

lesson_reminder は import 時に必須の環境変数を読むので、import より前に
ダミー値を入れておく必要がある。
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_ENV = {
    "SF_USERNAME": "dummy",
    "SF_PASSWORD": "dummy",
    "SF_SECURITY_TOKEN": "dummy",
    "GAS_URL": "https://example.com/exec",
    "ROSTER_TOKEN": "dummy-token",
    "SLACK_WEBHOOK": "https://example.com/hook",
    "SLACK_TEACHER_WEBHOOK": "https://example.com/hook",
    "COURSE_RULES": "標準コースL:2,標準コースS:1,短期コース:0",
    "SKIP_LESSON_KEYWORDS": "未定,社内",
}


def load(**overrides):
    """指定した環境変数で lesson_reminder を読み込み直す。"""
    for key in list(os.environ):
        if key in BASE_ENV or key in ("TRIAL_PATTERN", "TRIAL_EXCEPT_PATTERN"):
            del os.environ[key]
    os.environ.update(BASE_ENV)
    os.environ.update(overrides)
    if "lesson_reminder" in sys.modules:
        return importlib.reload(sys.modules["lesson_reminder"])
    return importlib.import_module("lesson_reminder")


def lesson(name):
    return {"コース名": name, "授業名": name}


@pytest.mark.parametrize(
    "name, expected",
    [
        ("[生徒]標準コースL(英):指導枠", 2),
        ("[生徒]標準コースS(数):指導枠", 1),
        ("[生徒]短期コース(国)", 0),
        ("[生徒]表に無いコース", 0),
    ],
)
def test_前倒し時間は判定表の順に決まる(name, expected):
    tr = load()
    assert tr.get_offset(lesson(name)) == expected


def test_体験回には前倒しを適用しない():
    tr = load()
    assert tr.get_offset(lesson("[生徒]標準コースL 体験")) == 0


def test_2回目の体験は通常回と同じ前倒しになる():
    tr = load()
    for suffix in ("体験2", "体験２", "体験②"):
        assert tr.get_offset(lesson(f"[生徒]標準コースL {suffix}")) == 2


def test_判定表が未設定なら前倒しはしない():
    tr = load(COURSE_RULES="")
    assert tr.get_offset(lesson("[生徒]標準コースL")) == 0


def test_表示ラベルは一致した部分だけを取り出す():
    tr = load()
    assert tr.course_label(lesson("[生徒]標準コースL(英):指導枠")) == "標準コースL"


def test_判定表に無ければ名称をそのまま表示する():
    tr = load()
    assert tr.course_label(lesson("[生徒]表に無いコース")) == "表に無いコース"


def test_除外キーワードを含む授業はスキップする():
    tr = load()
    assert tr.is_skipped_lesson("[生徒]未定 標準コースL") is True
    assert tr.is_skipped_lesson("[生徒]社内ミーティング") is True
    assert tr.is_skipped_lesson("[生徒]標準コースL") is False


def test_除外キーワードが未設定なら何もスキップしない():
    tr = load(SKIP_LESSON_KEYWORDS="")
    assert tr.is_skipped_lesson("[生徒]未定 標準コースL") is False


def test_不正な判定表は無視して残りを使う():
    tr = load(COURSE_RULES="標準コースL:あ,標準コースS:1,[:3")
    assert tr.get_offset(lesson("[生徒]標準コースL")) == 0
    assert tr.get_offset(lesson("[生徒]標準コースS")) == 1


def test_通知文に前倒し後の開始時刻が入る():
    tr = load()
    msg = tr.get_msg(
        {
            "コース名": "[山田太郎]標準コースL(英):指導枠",
            "授業名": "[山田太郎]標準コースL(英):指導枠",
            "生徒氏名": "山田太郎",
            "科目": "英",
            "開始時間": "16:00",
            "終了時間": "18:00",
            "担当": "佐藤花子",
        }
    )
    assert "標準コースL" in msg
    assert "14:00‐18:00" in msg
