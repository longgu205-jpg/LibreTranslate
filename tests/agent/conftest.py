import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest


@pytest.fixture(autouse=True)
def _fake_online(monkeypatch):
    """Default every API test to 'online' so no test touches the real network.

    Offline-lock tests override this with their own monkeypatch of
    ``agent.main.is_online``.
    """
    monkeypatch.setattr("agent.main.is_online", lambda *a, **kw: True)
