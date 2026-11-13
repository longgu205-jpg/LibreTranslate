import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient

from agent.main import app
from agent.mistral_client import MistralError

import pytest


@pytest.fixture(autouse=True)
def fake_api_key(monkeypatch):
    monkeypatch.setattr("agent.main.settings.mistral_api_key", "test-key")


class FakeClient:
    model = "mistral-large-latest"

    def chat(self, prompt, timeout, temperature=0.2):
        return "這是 __PROTECTED_BLOCK_0__ 與 __PROTECTED_BLOCK_1__ 的譯文。"

    def close(self):
        pass


class FailingClient:
    model = "mistral-large-latest"

    def chat(self, prompt, timeout, temperature=0.2):
        raise MistralError("Mistral API failed after retries: HTTP 500")

    def close(self):
        pass


def make_client():
    with TestClient(app) as test_client:
        return test_client


def test_health():
    client = make_client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_translate_passthrough_same_lang():
    client = make_client()
    response = client.post(
        "/translate",
        json={"text": "Hello", "source_lang": "en", "target_lang": "en"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "passthrough"
    assert body["translated_text"] == "Hello"


def test_translate_calls_mistral_and_restores_code():
    client = make_client()
    app.state.mistral_client = FakeClient()
    response = client.post(
        "/translate",
        json={"text": "This is `code`:\n```python\nprint(1)\n```", "target_lang": "zh-TW"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "cloud"
    assert "`code`" in body["translated_text"]
    assert "```python\nprint(1)\n```" in body["translated_text"]


def test_translate_returns_502_on_mistral_error():
    client = make_client()
    app.state.mistral_client = FailingClient()
    response = client.post("/translate", json={"text": "Hello", "target_lang": "zh-TW"})
    assert response.status_code == 502


def test_translate_rejects_bad_target_lang():
    client = make_client()
    response = client.post("/translate", json={"text": "Hello", "target_lang": "fr"})
    assert response.status_code == 422
