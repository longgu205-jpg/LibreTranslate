import logging
import time

from agent.cache import TranslationCache
from agent.code_protector import CodeProtector
from agent.config import settings
from agent.llm_client import LLMClient, LLMError
from agent.offline import OfflineEngine
from agent.prompt import PROMPT_VERSION, build_prompt

logger = logging.getLogger("agent.translator")


def estimate_timeout(text: str, short_text_chars: int = 300, short_timeout: float = 8.0) -> float:
    if len(text) < short_text_chars:
        return short_timeout
    estimated_tokens = max(1, int(len(text) * 0.75))
    return max(short_timeout, estimated_tokens / 50)


class TranslatorService:
    def __init__(
        self,
        client: LLMClient,
        cache: TranslationCache | None = None,
        offline: OfflineEngine | None = None,
        backup_client: LLMClient | None = None,
    ):
        self.client = client
        self.backup_client = backup_client
        self.cache = cache if settings.cache_enabled else None
        self.offline = offline if settings.offline_enabled else None

    def _cache_models(self) -> list[str]:
        models = [self.client.model]
        if self.backup_client is not None and self.backup_client.model != self.client.model:
            models.append(self.backup_client.model)
        return models

    def _offline_fallback(self, text, source_lang, target_lang, primary_error, started) -> dict:
        if self.offline is None:
            raise primary_error
        logger.warning("Cloud engines unavailable (%s), switching to offline engine", primary_error)
        offline_result = self.offline.translate(text, source_lang, target_lang)
        if offline_result:
            return {
                "translated_text": offline_result,
                "source": "offline_fallback",
                "model": "argos-offline",
                "source_lang": source_lang,
                "target_lang": target_lang,
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
            }
        reason = self.offline.last_error or "offline models not installed (run: python -m agent.download_models)"
        raise LLMError(f"{primary_error}; offline fallback failed: {reason}")

    def translate(self, text: str, source_lang: str, target_lang: str) -> dict:
        started = time.perf_counter()

        if self.cache is not None:
            for model_key in self._cache_models():
                cached = self.cache.get(source_lang, target_lang, model_key, PROMPT_VERSION, text)
                if cached is not None:
                    elapsed_ms = round((time.perf_counter() - started) * 1000)
                    return {
                        "translated_text": cached,
                        "source": "cache",
                        "model": model_key,
                        "source_lang": source_lang,
                        "target_lang": target_lang,
                        "elapsed_ms": elapsed_ms,
                    }

        protector = CodeProtector()
        protected_text = protector.protect(text)
        prompt = build_prompt(target_lang, protected_text)
        timeout = estimate_timeout(protected_text)

        used_client = self.client
        result_source = "cloud"
        try:
            raw_result = self.client.chat(prompt, timeout=timeout).strip()
        except LLMError as primary_error:
            if self.backup_client is not None:
                logger.warning(
                    "Primary LLM failed (%s), switching to backup engine %s",
                    primary_error, self.backup_client.model,
                )
                try:
                    raw_result = self.backup_client.chat(prompt, timeout=timeout).strip()
                    used_client = self.backup_client
                    result_source = "cloud_backup"
                except LLMError as backup_error:
                    return self._offline_fallback(
                        text, source_lang, target_lang,
                        LLMError(f"{primary_error}; backup engine: {backup_error}"), started,
                    )
            else:
                return self._offline_fallback(text, source_lang, target_lang, primary_error, started)

        if not protector.integrity_ok(raw_result):
            raise LLMError("Translation output lost one or more protected placeholders")

        restored = protector.restore(raw_result)

        if self.cache is not None:
            self.cache.put(source_lang, target_lang, used_client.model, PROMPT_VERSION, text, restored)

        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return {
            "translated_text": restored,
            "source": result_source,
            "model": used_client.model,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "elapsed_ms": elapsed_ms,
        }
