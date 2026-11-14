import logging
import socket
import threading
import time

import uvicorn

from agent.config import settings
from agent.main import app

logger = logging.getLogger("agent.desktop")


def pick_free_port(host: str | None = None) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host or settings.host, 0))
        return sock.getsockname()[1]


def start_server(port: int) -> uvicorn.Server:
    server = uvicorn.Server(
        uvicorn.Config(app, host=settings.host, port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.1)
    if not server.started:
        raise RuntimeError("FastAPI server failed to start within 15 seconds")
    return server


def run_desktop():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    port = pick_free_port()
    logger.info("Starting server on port %d", port)
    start_server(port)

    import webview

    webview.create_window(
        "混合翻譯 Agent",
        f"http://127.0.0.1:{port}/",
        width=1100,
        height=850,
        min_size=(700, 500),
    )
    webview.start()


if __name__ == "__main__":
    run_desktop()
