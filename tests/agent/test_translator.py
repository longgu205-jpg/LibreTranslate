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
