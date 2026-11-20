import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from agent.offline import OfflineEngine, apply_terminology


def test_terminology_replacement():
    assert apply_terminology("這是視頻和數據") == "這是影片和資料"
    assert apply_terminology("服務器上的代碼") == "伺服器上的程式碼"
    assert apply_terminology("沒有匹配詞") == "沒有匹配詞"


@pytest.fixture
def engine():
    engine = OfflineEngine()
    pytest.importorskip("opencc")
    engine._ensure_converters()
    return engine


def test_en_to_zh_tw_pipeline(engine, monkeypatch):
    monkeypatch.setattr(engine, "available", lambda: True)
    monkeypatch.setattr(
        engine,
        "_argos_translate",
        lambda text, f, t: "这是软件，数据保存在服务器上。使用默认设置优化质量。",
    )
    result = engine.translate("This is software.", "en", "zh-TW")
    assert result is not None
    assert "軟體" in result
    assert "資料" in result
    assert "伺服器" in result
    assert "預設" in result
    for simplified in ["软件", "数据", "服务器", "默认"]:
        assert simplified not in result


def test_zh_to_en_converts_to_simplified_first(engine, monkeypatch):
    captured = {}

    def fake_argos(text, f, t):
        captured["text"] = text
        return "Hello"

    monkeypatch.setattr(engine, "available", lambda: True)
    monkeypatch.setattr(engine, "_argos_translate", fake_argos)
    result = engine.translate("軟體工程", "zh", "en")
    assert result == "Hello"
    assert captured["text"] == "软体工程"


def test_returns_none_when_models_missing(engine, monkeypatch):
    monkeypatch.setattr(engine, "available", lambda: True)
    monkeypatch.setattr(engine, "_argos_translate", lambda text, f, t: None)
    assert engine.translate("Hello", "en", "zh-TW") is None


def test_returns_none_on_empty(engine):
    assert engine.translate("   ", "en", "zh-TW") is None


def test_returns_none_for_unsupported_direction(engine, monkeypatch):
    monkeypatch.setattr(engine, "available", lambda: True)
    assert engine.translate("Hello", "zh-TW", "zh-TW") is None
