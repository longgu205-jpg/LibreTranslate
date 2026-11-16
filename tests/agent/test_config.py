import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import importlib
import os

from agent import config


def test_config_reads_bom_env_next_to_exe(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"\xef\xbb\xbf" + b"LLM_API_KEY=gsk_bomtest\n")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "FormosalabTranslator.exe"))

    old = os.environ.get("LLM_API_KEY")
    try:
        importlib.reload(config)
        assert config.settings.llm_api_key == "gsk_bomtest"
    finally:
        if old is None:
            os.environ.pop("LLM_API_KEY", None)
        else:
            os.environ["LLM_API_KEY"] = old
        importlib.reload(config)
        assert config.settings is not None


def test_gemini_provider_defaults_present():
    from agent.config import PROVIDER_DEFAULTS

    assert "gemini" in PROVIDER_DEFAULTS
    assert PROVIDER_DEFAULTS["gemini"]["base_url"].startswith(
        "https://generativelanguage.googleapis.com"
    )
    assert PROVIDER_DEFAULTS["gemini"]["model"]


def test_stt_defaults_reuse_primary_key(monkeypatch):
    import importlib

    monkeypatch.delenv("STT_API_KEY", raising=False)
    monkeypatch.delenv("STT_MODEL", raising=False)
    monkeypatch.delenv("STT_BASE_URL", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "primary-key")
    import agent.config as config_module

    importlib.reload(config_module)
    try:
        assert config_module.settings.stt_api_key == "primary-key"
        assert config_module.settings.stt_model == "whisper-large-v3-turbo"
        assert config_module.settings.backup_api_key == ""
    finally:
        importlib.reload(config_module)
