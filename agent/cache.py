import hashlib
import sqlite3
import threading
import time
from pathlib import Path

from agent.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS translations (
    cache_key TEXT PRIMARY KEY,
    source_lang TEXT NOT NULL,
    target_lang TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    source_text TEXT NOT NULL,
    translated_text TEXT NOT NULL,
    created_at REAL NOT NULL
)
"""


class TranslationCache:
    def __init__(self, db_path: str | None = None):
        self.db_path = Path(db_path or settings.cache_db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    @staticmethod
    def make_key(source_lang: str, target_lang: str, model: str, prompt_version: str, text: str) -> str:
        normalized = " ".join(text.split())
        payload = "|".join([source_lang, target_lang, model, prompt_version, normalized])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, source_lang: str, target_lang: str, model: str, prompt_version: str, text: str) -> str | None:
        key = self.make_key(source_lang, target_lang, model, prompt_version, text)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT translated_text FROM translations WHERE cache_key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def put(
        self,
        source_lang: str,
        target_lang: str,
        model: str,
        prompt_version: str,
        text: str,
        translated_text: str,
    ) -> None:
        key = self.make_key(source_lang, target_lang, model, prompt_version, text)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO translations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key, source_lang, target_lang, model, prompt_version, text, translated_text, time.time()),
            )

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0]
