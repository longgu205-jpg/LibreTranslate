import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    mistral_api_key: str = os.getenv("MISTRAL_API_KEY", "")
    mistral_model: str = os.getenv("MISTRAL_MODEL", "mistral-large-latest")
    host: str = os.getenv("AGENT_HOST", "127.0.0.1")
    port: int = int(os.getenv("AGENT_PORT", "8000"))
    connect_timeout: float = float(os.getenv("MISTRAL_CONNECT_TIMEOUT", "2"))
    short_text_timeout: float = float(os.getenv("MISTRAL_SHORT_TIMEOUT", "8"))
    max_retries: int = int(os.getenv("MISTRAL_MAX_RETRIES", "2"))
    short_text_chars: int = int(os.getenv("SHORT_TEXT_CHARS", "300"))
    timeout_per_50_tokens: float = float(os.getenv("TIMEOUT_PER_50_TOKENS", "1"))


settings = Settings()
