import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from agent.cache import TranslationCache
from agent.config import settings
from agent.llm_client import LLMError
from agent.translator import TranslatorService


class FakeClient:
    model = "test-model"
    calls = 0

    def chat(self, prompt, timeout, temperature=0.2):
        FakeClient.calls += 1
        return "譯文。"

    def close(self):
        pass


@pytest.fixture(autouse=True)
def enable_cache(monkeypatch):
    monkeypatch.setattr(settings, "cache_enabled", True)


def test_first_call_is_cloud_second_is_cache(tmp_path):
    FakeClient.calls = 0
    service = TranslatorService(FakeClient(), TranslationCache(db_path=str(tmp_path / "cache.db")))

    first = service.translate("Hello world", "en", "zh-TW")
    assert first["source"] == "cloud"
    assert first["translated_text"] == "譯文。"

    second = service.translate("Hello world", "en", "zh-TW")
    assert second["source"] == "cache"
    assert second["translated_text"] == "譯文。"
    assert FakeClient.calls == 1


def test_failed_llm_call_is_not_cached(tmp_path):
    class FailingOnce:
        model = "test-model"

        def __init__(self):
            self.calls = 0

        def chat(self, prompt, timeout, temperature=0.2):
            self.calls += 1
            if self.calls == 1:
                raise LLMError("boom")
            return "譯文。"

    client = FailingOnce()
    service = TranslatorService(client, TranslationCache(db_path=str(tmp_path / "cache.db")))

    with pytest.raises(LLMError):
        service.translate("Hello", "en", "zh-TW")

    result = service.translate("Hello", "en", "zh-TW")
    assert result["source"] == "cloud"


def test_cache_disabled_bypasses(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_enabled", False)
    FakeClient.calls = 0
    service = TranslatorService(FakeClient(), TranslationCache(db_path=str(tmp_path / "cache.db")))

    service.translate("Hello", "en", "zh-TW")
    second = service.translate("Hello", "en", "zh-TW")
    assert second["source"] == "cloud"
    assert FakeClient.calls == 2


class OfflineStub:
    def translate(self, text, source_lang, target_lang):
        return "離線譯文。"


class OfflineUnavailable:
    def translate(self, text, source_lang, target_lang):
        return None


def test_llm_error_falls_back_to_offline(tmp_path):
    class AlwaysFails:
        model = "test-model"

        def chat(self, prompt, timeout, temperature=0.2):
            raise LLMError("network down")

    cache = TranslationCache(db_path=str(tmp_path / "cache.db"))
    service = TranslatorService(AlwaysFails(), cache, OfflineStub())

    result = service.translate("Hello", "en", "zh-TW")
    assert result["source"] == "offline_fallback"
    assert result["translated_text"] == "離線譯文。"
    assert result["model"] == "argos-offline"


def test_offline_result_is_not_cached(tmp_path):
    class AlwaysFails:
        model = "test-model"

        def chat(self, prompt, timeout, temperature=0.2):
            raise LLMError("network down")

    cache = TranslationCache(db_path=str(tmp_path / "cache.db"))
    service = TranslatorService(AlwaysFails(), cache, OfflineStub())

    service.translate("Hello", "en", "zh-TW")
    assert cache.get("en", "zh-TW", "test-model", "v1", "Hello") is None


def test_offline_unavailable_reraises_llm_error(tmp_path):
    class AlwaysFails:
        model = "test-model"

        def chat(self, prompt, timeout, temperature=0.2):
            raise LLMError("network down")

    service = TranslatorService(AlwaysFails(), TranslationCache(db_path=str(tmp_path / "cache.db")), OfflineUnavailable())

    with pytest.raises(LLMError):
        service.translate("Hello", "en", "zh-TW")
