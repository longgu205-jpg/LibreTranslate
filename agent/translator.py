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
    ):
        self.client = client
        self.cache = cache if settings.cache_enabled else None
        self.offline = offline if settings.offline_enabled else None

    def translate(self, text: str, source_lang: str, target_lang: str) -> dict:
        started = time.perf_counter()

        if self.cache is not None:
            cached = self.cache.get(source_lang, target_lang, self.client.model, PROMPT_VERSION, text)
            if cached is not None:
                elapsed_ms = round((time.perf_counter() - started) * 1000)
                return {
                    "translated_text": cached,
                    "source": "cache",
                    "model": self.client.model,
                    "source_lang": source_lang,
                    "target_lang": target_lang,
                    "elapsed_ms": elapsed_ms,
                }

        protector = CodeProtector()
        protected_text = protector.protect(text)
        prompt = build_prompt(target_lang, protected_text)
        timeout = estimate_timeout(protected_text)

        try:
            raw_result = self.client.chat(prompt, timeout=timeout).strip()
        except LLMError as exc:
            if self.offline is not None:
                logger.warning("LLM unavailable (%s), switching to offline engine", exc)
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
                raise LLMError(f"{exc}; offline fallback failed: {reason}")
            raise

        if not protector.integrity_ok(raw_result):
            raise LLMError("Translation output lost one or more protected placeholders")

        restored = protector.restore(raw_result)

        if self.cache is not None:
            self.cache.put(source_lang, target_lang, self.client.model, PROMPT_VERSION, text, restored)

        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return {
            "translated_text": restored,
            "source": "cloud",
            "model": self.client.model,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "elapsed_ms": elapsed_ms,
        }
