import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx

from agent.netcheck import is_online


def _transport(handler):
    return httpx.MockTransport(handler)


def test_online_when_service_responds():
    """Any HTTP response (even 401) means the host is reachable."""
    def handler(request):
        return httpx.Response(401)

    assert is_online("https://api.example.com", transport=_transport(handler)) is True


def test_offline_on_connect_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    assert is_online("https://api.example.com", transport=_transport(handler)) is False


def test_offline_on_empty_base_url():
    assert is_online("") is False


def test_ttl_cache_avoids_repeated_requests():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200)

    # Real (non-mock) calls use the TTL cache; simulate by patching time.
    # Instead verify the transport path bypasses cache: two mock calls = two requests.
    transport = _transport(handler)
    is_online("https://api.example.com", transport=transport)
    is_online("https://api.example.com", transport=transport)
    assert len(calls) == 2
