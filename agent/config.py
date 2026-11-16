import os
import sys
from pathlib import Path

from dotenv import load_dotenv

if getattr(sys, "frozen", False):
    load_dotenv(Path(sys.executable).parent / ".env", encoding="utf-8-sig")
load_dotenv(encoding="utf-8-sig")

_BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()

PROVIDER_DEFAULTS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-120b",
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "model": "mistral-large-latest",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
    },
}


def _provider_default(provider: str, field: str) -> str:
    return PROVIDER_DEFAULTS.get(provider, PROVIDER_DEFAULTS["groq"])[field]


class Settings:
    llm_provider: str = os.getenv("LLM_PROVIDER", "groq").lower()
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "") or _provider_default(os.getenv("LLM_PROVIDER", "groq").lower(), "model")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "") or _provider_default(os.getenv("LLM_PROVIDER", "groq").lower(), "base_url")
    # Backup engine (2b): enabled only when its API key is set. Empty key = disabled.
    backup_provider: str = os.getenv("LLM_BACKUP_PROVIDER", "gemini").lower()
    backup_api_key: str = os.getenv("LLM_API_KEY_GEMINI", "")
    backup_model: str = os.getenv("LLM_BACKUP_MODEL", "") or _provider_default(backup_provider, "model")
    backup_base_url: str = os.getenv("LLM_BACKUP_BASE_URL", "") or _provider_default(backup_provider, "base_url")
    # Speech-to-text (2a): defaults reuse the primary Groq key and endpoint.
    stt_api_key: str = os.getenv("STT_API_KEY", "") or os.getenv("LLM_API_KEY", "")
    stt_base_url: str = os.getenv("STT_BASE_URL", "") or _provider_default(os.getenv("LLM_PROVIDER", "groq").lower(), "base_url")
    stt_model: str = os.getenv("STT_MODEL", "whisper-large-v3-turbo")
    stt_timeout: float = float(os.getenv("STT_TIMEOUT", "120"))
    stt_max_mb: int = int(os.getenv("STT_MAX_MB", "25"))
    host: str = os.getenv("AGENT_HOST", "127.0.0.1")
    port: int = int(os.getenv("AGENT_PORT", "8000"))
    connect_timeout: float = float(os.getenv("LLM_CONNECT_TIMEOUT", "2"))
    short_text_timeout: float = float(os.getenv("LLM_SHORT_TIMEOUT", "8"))
    max_retries: int = int(os.getenv("LLM_MAX_RETRIES", "2"))
    cache_db_path: str = os.getenv("CACHE_DB_PATH", str(_BASE_DIR / "data" / "cache.db"))
    cache_enabled: bool = os.getenv("CACHE_ENABLED", "1") == "1"
    offline_enabled: bool = os.getenv("OFFLINE_ENABLED", "1") == "1"
    voice_enabled: bool = os.getenv("VOICE_ENABLED", "1") == "1"


settings = Settings()
