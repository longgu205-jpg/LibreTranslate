import logging

import httpx

from agent.config import settings

logger = logging.getLogger("agent.llm")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        connect_timeout: float | None = None,
        max_retries: int | None = None,
    ):
        self.api_key = api_key or settings.llm_api_key
        self.model = model or settings.llm_model
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.connect_timeout = connect_timeout or settings.connect_timeout
        self.max_retries = settings.max_retries if max_retries is None else max_retries
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    def chat(self, prompt: str, timeout: float, temperature: float = 0.2) -> str:
        payload = {
            "model": self.model,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        timeout_config = httpx.Timeout(timeout, connect=self.connect_timeout)
        last_error = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._client.post(
                    f"{self.base_url}/chat/completions", json=payload, timeout=timeout_config
                )
            except httpx.HTTPError as exc:
                last_error = f"network error: {exc}"
                logger.warning("LLM attempt %d failed: %s", attempt, last_error)
                continue

            if response.status_code == 200:
                return response.json()["choices"][0]["message"]["content"]

            last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            logger.warning("LLM attempt %d failed: %s", attempt, last_error)

            if response.status_code not in RETRYABLE_STATUS:
                break

        raise LLMError(f"LLM API failed after retries: {last_error}")

    def close(self):
        self._client.close()
