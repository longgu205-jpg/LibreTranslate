import time

from agent.code_protector import CodeProtector
from agent.llm_client import LLMClient, LLMError
from agent.prompt import build_prompt


def estimate_timeout(text: str, short_text_chars: int = 300, short_timeout: float = 8.0) -> float:
    if len(text) < short_text_chars:
        return short_timeout
    estimated_tokens = max(1, int(len(text) * 0.75))
    return max(short_timeout, estimated_tokens / 50)


class TranslatorService:
    def __init__(self, client: LLMClient):
        self.client = client

    def translate(self, text: str, source_lang: str, target_lang: str) -> dict:
        started = time.perf_counter()
        protector = CodeProtector()
        protected_text = protector.protect(text)
        prompt = build_prompt(target_lang, protected_text)
        timeout = estimate_timeout(protected_text)

        raw_result = self.client.chat(prompt, timeout=timeout).strip()

        if not protector.integrity_ok(raw_result):
            raise LLMError("Translation output lost one or more protected placeholders")

        restored = protector.restore(raw_result)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return {
            "translated_text": restored,
            "source": "cloud",
            "model": self.client.model,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "elapsed_ms": elapsed_ms,
        }
