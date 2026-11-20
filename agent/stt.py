import logging

import httpx

from agent.config import settings

logger = logging.getLogger("agent.stt")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Extensions accepted by OpenAI-compatible /audio/transcriptions endpoints.
ALLOWED_AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".webm", ".ogg", ".oga",
    ".flac", ".mp4", ".mpeg", ".mpga", ".opus",
}


class STTError(Exception):
    pass


class TranscriptionClient:
    """OpenAI-compatible speech-to-text client (Groq Whisper)."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        connect_timeout: float | None = None,
        max_retries: int | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.api_key = api_key or settings.stt_api_key
        self.model = model or settings.stt_model
        self.base_url = (base_url or settings.stt_base_url).rstrip("/")
        self.connect_timeout = connect_timeout or settings.connect_timeout
        self.max_retries = settings.max_retries if max_retries is None else max_retries
        self.timeout = timeout or settings.stt_timeout
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {self.api_key}"},
            transport=transport,
        )

    def transcribe(
        self,
        audio_bytes: bytes,
        filename: str = "audio.webm",
        language: str | None = None,
        timeout: float | None = None,
    ) -> str:
        """Send audio to the STT endpoint and return the transcript text."""
        data = {"model": self.model, "response_format": "json"}
        if language:
            data["language"] = language

        timeout_config = httpx.Timeout(
            timeout or self.timeout, connect=self.connect_timeout
        )
        last_error = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._client.post(
                    f"{self.base_url}/audio/transcriptions",
                    files={"file": (filename, audio_bytes)},
                    data=data,
                    timeout=timeout_config,
                )
            except httpx.HTTPError as exc:
                last_error = f"network error: {exc}"
                logger.warning("STT attempt %d failed: %s", attempt, last_error)
                continue

            if response.status_code == 200:
                text = response.json().get("text", "").strip()
                logger.info("STT done: %d bytes audio -> %d chars", len(audio_bytes), len(text))
                return text

            last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            logger.warning("STT attempt %d failed: %s", attempt, last_error)

            if response.status_code not in RETRYABLE_STATUS:
                break

        raise STTError(f"STT API failed after retries: {last_error}")

    def close(self):
        self._client.close()
