import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.cache import TranslationCache


def test_put_and_get_roundtrip(tmp_path):
    cache = TranslationCache(db_path=str(tmp_path / "cache.db"))
    cache.put("en", "zh-TW", "m", "v1", "Hello world", "哈囉，世界！")
    assert cache.get("en", "zh-TW", "m", "v1", "Hello world") == "哈囉，世界！"


def test_miss_returns_none(tmp_path):
    cache = TranslationCache(db_path=str(tmp_path / "cache.db"))
    assert cache.get("en", "zh-TW", "m", "v1", "Never stored") is None


def test_whitespace_is_normalized(tmp_path):
    cache = TranslationCache(db_path=str(tmp_path / "cache.db"))
    cache.put("en", "zh-TW", "m", "v1", "Hello   world", "哈囉")
    assert cache.get("en", "zh-TW", "m", "v1", "Hello world") == "哈囉"


def test_key_changes_with_prompt_version(tmp_path):
    cache = TranslationCache(db_path=str(tmp_path / "cache.db"))
    cache.put("en", "zh-TW", "m", "v1", "Hello", "舊版翻譯")
    assert cache.get("en", "zh-TW", "m", "v2", "Hello") is None


def test_key_changes_with_model(tmp_path):
    cache = TranslationCache(db_path=str(tmp_path / "cache.db"))
    cache.put("en", "zh-TW", "model-a", "v1", "Hello", "A 的翻譯")
    assert cache.get("en", "zh-TW", "model-b", "v1", "Hello") is None


def test_key_changes_with_direction(tmp_path):
    cache = TranslationCache(db_path=str(tmp_path / "cache.db"))
    cache.put("en", "zh-TW", "m", "v1", "Hello", "哈囉")
    assert cache.get("zh-TW", "en", "m", "v1", "Hello") is None


def test_count(tmp_path):
    cache = TranslationCache(db_path=str(tmp_path / "cache.db"))
    assert cache.count() == 0
    cache.put("en", "zh-TW", "m", "v1", "Hello", "哈囉")
    assert cache.count() == 1
