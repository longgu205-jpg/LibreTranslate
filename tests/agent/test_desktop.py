import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx

from agent.desktop import pick_free_port, start_server


def test_pick_free_port_returns_open_port():
    port = pick_free_port()
    assert isinstance(port, int)
    assert 1024 <= port <= 65535


def test_pick_free_port_returns_distinct_ports():
    assert pick_free_port() != pick_free_port()


def test_start_server_serves_health_and_index():
    port = pick_free_port()
    start_server(port)
    health = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5)
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    index = httpx.get(f"http://127.0.0.1:{port}/", timeout=5)
    assert index.status_code == 200
    assert "混合翻譯 Agent" in index.text


def test_write_pdf_file_decodes_base64(tmp_path):
    import base64

    from agent.desktop import write_pdf_file

    payload = b"%PDF-1.4 test bytes"
    target = tmp_path / "out.pdf"
    write_pdf_file(str(target), base64.b64encode(payload).decode())
    assert target.read_bytes() == payload
