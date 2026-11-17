"""Lightweight connectivity check used to lock voice features while offline.

Any HTTP response (even 401/404) means DNS + TCP (+ TLS) worked, so the
speech services are reachable. Only connection-level failures mean offline.
Results are cached for a few seconds to keep /health polling cheap.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

logger = logging.getLogger(__name__)

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, bool]] = {}
_DEFAULT_TTL = 4.0
_DEFAULT_TIMEOUT = 1.5


def is_online(
    base_url: str,
    timeout: float = _DEFAULT_TIMEOUT,
    ttl: float = _DEFAULT_TTL,
    transport: httpx.BaseTransport | None = None,
) -> bool:
    """Return True when the service host is reachable.

    ``transport`` is for tests (httpx.MockTransport); when given, the TTL
    cache is bypassed so each call performs a real (mocked) request.
    """
    if not base_url:
        return False

    if transport is None:
        now = time.monotonic()
        with _cache_lock:
            cached = _cache.get(base_url)
        if cached is not None and now - cached[0] < ttl:
            return cached[1]

    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            client.get(base_url)
        online = True
    except Exception as exc:
        online = False
        logger.debug("Connectivity check to %s failed: %s", base_url, exc)

    if transport is None:
        with _cache_lock:
            _cache[base_url] = (time.monotonic(), online)
    return online
