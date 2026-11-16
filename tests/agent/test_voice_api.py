import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from fastapi.testclient import TestClient

from agent.main import app
from agent.stt import STTError


class FakeSTT:
    model = "whisper-large-v3-turbo"

    def __init__(self, text="Hello world. This is a test."):
        self.text = text
        self.calls = []

    def transcribe(self, audio_bytes, filename="audio.webm", language=None, timeout=None):
        self.calls.append({"size": len(audio_bytes), "filename": filename, "language": language})
        return self.text

    def close(self):
        pass


class FailingSTT:
    model = "whisper-large-v3-turbo"

    def transcribe(self, audio_bytes, filename="audio.webm", language=None, timeout=None):
        raise STTError("STT API failed after retries: HTTP 429")

    def close(self):
        pass


class FakeClient:
    model = "openai/gpt-oss-120b"

    def chat(self, prompt, timeout, temperature=0.2):
        return "這是測試譯文。"

    def close(self):
        pass


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr("agent.main.settings.llm_api_key", "test-key")
    monkeypatch.setattr("agent.main.settings.cache_db_path", str(tmp_path / "cache.db"))
    monkeypatch.setattr("agent.main.settings.voice_enabled", True)


def make_client():
    with TestClient(app) as test_client:
        return test_client


def test_transcribe_returns_text():
    client = make_client()
    app.state.stt_client = FakeSTT("Hello there.")
    response = client.post(
        "/transcribe",
        files={"file": ("meeting.mp3", b"fake-audio", "audio/mpeg")},
        data={"language": "en"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "Hello there."
    assert body["model"] == "whisper-large-v3-turbo"
    assert "elapsed_ms" in body


def test_transcribe_503_without_stt_client():
    client = make_client()
    app.state.stt_client = None
    response = client.post(
        "/transcribe", files={"file": ("a.mp3", b"x", "audio/mpeg")},
    )
    assert response.status_code == 503


def test_transcribe_rejects_unknown_extension():
    client = make_client()
    app.state.stt_client = FakeSTT()
    response = client.post(
        "/transcribe", files={"file": ("song.txt", b"x", "text/plain")},
    )
    assert response.status_code == 400
    assert "不支援的音訊格式" in response.json()["detail"]


def test_transcribe_rejects_empty_audio():
    client = make_client()
    app.state.stt_client = FakeSTT()
    response = client.post(
        "/transcribe", files={"file": ("a.mp3", b"", "audio/mpeg")},
    )
    assert response.status_code == 400


def test_translate_voice_full_flow():
    client = make_client()
    app.state.stt_client = FakeSTT("Please restart the server.")
    app.state.llm_client = FakeClient()
    app.state.backup_client = None
    response = client.post(
        "/translate/voice",
        files={"file": ("meeting.webm", b"fake-audio", "audio/webm")},
        data={"source_lang": "en", "target_lang": "zh-TW"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["transcript"] == "Please restart the server."
    assert body["translated_text"] == "這是測試譯文。"
    assert body["source"] == "cloud"
    assert "stt_elapsed_ms" in body


def test_translate_voice_auto_detects_language():
    client = make_client()
    app.state.stt_client = FakeSTT("這是中文測試。")
    app.state.llm_client = FakeClient()
    app.state.backup_client = None
    response = client.post(
        "/translate/voice",
        files={"file": ("a.webm", b"audio", "audio/webm")},
        data={"source_lang": "auto", "target_lang": "en"},
    )
    assert response.status_code == 200
    assert response.json()["source_lang"] == "zh"


def test_translate_voice_same_lang_passthrough():
    client = make_client()
    app.state.stt_client = FakeSTT("Hello there")
    app.state.llm_client = FakeClient()
    app.state.backup_client = None
    response = client.post(
        "/translate/voice",
        files={"file": ("a.webm", b"audio", "audio/webm")},
        data={"source_lang": "en", "target_lang": "en"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "passthrough"
    assert body["translated_text"] == "Hello there"


def test_translate_voice_502_on_stt_error():
    client = make_client()
    app.state.stt_client = FailingSTT()
    app.state.llm_client = FakeClient()
    app.state.backup_client = None
    response = client.post(
        "/translate/voice",
        files={"file": ("a.webm", b"audio", "audio/webm")},
    )
    assert response.status_code == 502
    assert "STT API failed" in response.json()["detail"]


def test_translate_voice_422_on_empty_transcript():
    client = make_client()
    app.state.stt_client = FakeSTT("")
    app.state.llm_client = FakeClient()
    app.state.backup_client = None
    response = client.post(
        "/translate/voice",
        files={"file": ("a.webm", b"audio", "audio/webm")},
    )
    assert response.status_code == 422


def test_translate_voice_disabled():
    client = make_client()
    monkey_enabled = "agent.main.settings.voice_enabled"
    import agent.main as main_module
    original = main_module.settings.voice_enabled
    main_module.settings.voice_enabled = False
    try:
        response = client.post(
            "/translate/voice",
            files={"file": ("a.webm", b"audio", "audio/webm")},
        )
        assert response.status_code == 403
    finally:
        main_module.settings.voice_enabled = original
