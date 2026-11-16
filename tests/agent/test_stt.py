import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
import pytest

from agent.stt import ALLOWED_AUDIO_EXTENSIONS, STTError, TranscriptionClient


def make_client(handler, max_retries=2):
    return TranscriptionClient(
        api_key="test-key",
        model="whisper-large-v3-turbo",
        base_url="https://stt.test/v1",
        transport=httpx.MockTransport(handler),
        max_retries=max_retries,
    )


def test_transcribe_success():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("Authorization")
        seen["content_type"] = request.headers.get("Content-Type", "")
        return httpx.Response(200, json={"text": "  Hello world.  "})

    client = make_client(handler)
    text = client.transcribe(b"audio-bytes", "meeting.wav", language="en")
    assert text == "Hello world."
    assert seen["path"] == "/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer test-key"
    assert "multipart/form-data" in seen["content_type"]


def test_transcribe_omits_language_when_auto():
    seen = {}

    def handler(request):
        content = request.content.decode("utf-8", errors="replace")
        seen["has_language"] = 'name="language"' in content
        return httpx.Response(200, json={"text": "hi"})

    client = make_client(handler)
    client.transcribe(b"audio", "a.webm", language=None)
    assert seen["has_language"] is False


def test_transcribe_retries_on_429_then_succeeds():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json={"text": "recovered"})

    client = make_client(handler, max_retries=2)
    assert client.transcribe(b"audio", "a.mp3") == "recovered"
    assert attempts["n"] == 2


def test_transcribe_fast_fails_on_401():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(401, text="bad key")

    client = make_client(handler, max_retries=3)
    with pytest.raises(STTError):
        client.transcribe(b"audio", "a.mp3")
    assert attempts["n"] == 1


def test_transcribe_raises_after_network_errors():
    def handler(request):
        raise httpx.ConnectError("boom")

    client = make_client(handler, max_retries=2)
    with pytest.raises(STTError, match="network error"):
        client.transcribe(b"audio", "a.mp3")


def test_allowed_audio_extensions():
    for ext in (".mp3", ".wav", ".m4a", ".webm", ".ogg", ".flac", ".mp4", ".opus"):
        assert ext in ALLOWED_AUDIO_EXTENSIONS
