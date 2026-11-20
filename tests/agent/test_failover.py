import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from agent.cache import TranslationCache
from agent.config import settings
from agent.llm_client import LLMError
from agent.translator import TranslatorService


class FailingClient:
    model = "groq-model"

    def chat(self, prompt, timeout, temperature=0.2):
        raise LLMError("LLM API failed after retries: HTTP 429")

    def close(self):
        pass


class BackupClient:
    model = "gemini-2.5-flash"

    def __init__(self):
        self.calls = 0

    def chat(self, prompt, timeout, temperature=0.2):
        self.calls += 1
        return "備援譯文。"

    def close(self):
        pass


@pytest.fixture(autouse=True)
def enable_cache(monkeypatch):
    monkeypatch.setattr(settings, "cache_enabled", True)
    monkeypatch.setattr(settings, "offline_enabled", True)


def test_backup_used_when_primary_fails(tmp_path):
    backup = BackupClient()
    service = TranslatorService(
        FailingClient(), TranslationCache(db_path=str(tmp_path / "c.db")), None, backup,
    )
    result = service.translate("Hello world", "en", "zh-TW")
    assert result["source"] == "cloud_backup"
    assert result["model"] == "gemini-2.5-flash"
    assert result["translated_text"] == "備援譯文。"
    assert backup.calls == 1


def test_backup_result_is_cached_under_backup_model(tmp_path):
    cache = TranslationCache(db_path=str(tmp_path / "c.db"))
    backup = BackupClient()
    service = TranslatorService(FailingClient(), cache, None, backup)
    service.translate("Hello world", "en", "zh-TW")

    # Same text again: served from cache without calling the backup engine.
    second = service.translate("Hello world", "en", "zh-TW")
    assert second["source"] == "cache"
    assert second["model"] == "gemini-2.5-flash"
    assert backup.calls == 1


def test_offline_used_when_both_engines_fail(tmp_path):
    class FailingBackup:
        model = "gemini-2.5-flash"

        def chat(self, prompt, timeout, temperature=0.2):
            raise LLMError("backup failed too")

        def close(self):
            pass

    class WorkingOffline:
        last_error = None

        def translate(self, text, source_lang, target_lang):
            return "離線譯文。"

    service = TranslatorService(
        FailingClient(), None, WorkingOffline(), FailingBackup(),
    )
    result = service.translate("Hello world", "en", "zh-TW")
    assert result["source"] == "offline_fallback"
    assert result["model"] == "argos-offline"


def test_error_mentions_both_engines_when_all_fail(tmp_path):
    class FailingBackup:
        model = "gemini-2.5-flash"
        last_error = None

        def chat(self, prompt, timeout, temperature=0.2):
            raise LLMError("backup down")

        def close(self):
            pass

    class BrokenOffline:
        last_error = "no models installed"

        def translate(self, text, source_lang, target_lang):
            return None

    service = TranslatorService(
        FailingClient(), None, BrokenOffline(), FailingBackup(),
    )
    with pytest.raises(LLMError) as exc_info:
        service.translate("Hello world", "en", "zh-TW")
    assert "backup down" in str(exc_info.value) or "HTTP 429" in str(exc_info.value)
    assert "backup engine" in str(exc_info.value)
    assert "offline fallback failed" in str(exc_info.value)


def test_no_backup_keeps_original_behaviour(tmp_path):
    class WorkingOffline:
        last_error = None

        def translate(self, text, source_lang, target_lang):
            return "離線譯文。"

    service = TranslatorService(FailingClient(), None, WorkingOffline(), None)
    result = service.translate("Hello world", "en", "zh-TW")
    assert result["source"] == "offline_fallback"
