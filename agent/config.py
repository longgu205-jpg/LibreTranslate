import os

from dotenv import load_dotenv

load_dotenv()

PROVIDER_DEFAULTS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-120b",
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "model": "mistral-large-latest",
    },
}


class Settings:
    llm_provider: str = os.getenv("LLM_PROVIDER", "groq").lower()
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "") or PROVIDER_DEFAULTS[os.getenv("LLM_PROVIDER", "groq").lower()]["model"]
    llm_base_url: str = os.getenv("LLM_BASE_URL", "") or PROVIDER_DEFAULTS[os.getenv("LLM_PROVIDER", "groq").lower()]["base_url"]
    host: str = os.getenv("AGENT_HOST", "127.0.0.1")
    port: int = int(os.getenv("AGENT_PORT", "8000"))
    connect_timeout: float = float(os.getenv("LLM_CONNECT_TIMEOUT", "2"))
    short_text_timeout: float = float(os.getenv("LLM_SHORT_TIMEOUT", "8"))
    max_retries: int = int(os.getenv("LLM_MAX_RETRIES", "2"))
    cache_db_path: str = os.getenv("CACHE_DB_PATH", "data/cache.db")
    cache_enabled: bool = os.getenv("CACHE_ENABLED", "1") == "1"


settings = Settings()
