"""Run Cohandle on this machine, as a desktop app.

Double-click `run-cohandle.cmd` and this starts the server, waits until it
is genuinely answering, and opens your browser at the queue. Close the
window to stop it.

Nothing about this is a different Cohandle. It is the same FastAPI app
the deploy runs, served to localhost only, which is the point: hosting it
costs nothing when the host is a laptop you already own, and the intake
worker keeps running the whole time the window is open.

Bound to 127.0.0.1 deliberately. Anything else would put an app with no
login on the office network.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HOST = "127.0.0.1"
PREFERRED_PORT = 8765


def pick_port() -> int:
    """The usual port, or any free one if something already has it."""
    for candidate in (PREFERRED_PORT, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((HOST, candidate))
                return sock.getsockname()[1]
            except OSError:
                continue
    raise SystemExit("no free port to listen on")


def open_when_ready(url: str, health: str) -> None:
    """Open the browser once the server answers, not a moment before.

    A fixed sleep would open an error page on a cold Neon compute, which
    can take a few seconds to wake.
    """
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health, timeout=2) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.3)
    print(f"server did not come up in time; try {url} yourself")


def main() -> int:
    try:
        from app.config import settings
    except ImportError:
        print("Dependencies are missing. Run this once:\n")
        print("    pip install -r requirements.txt\n")
        return 1

    if not settings().database_url:
        print("DATABASE_URL is not set, so there is no database to talk to.\n")
        print("Copy .env.example to .env and fill it in, or run:\n")
        print("    python scripts/setkey.py DATABASE_URL\n")
        return 1

    port = pick_port()
    url = f"http://{HOST}:{port}/"
    print(f"Cohandle is starting on {url}")
    print("Close this window, or press Ctrl+C, to stop it.\n")

    threading.Thread(target=open_when_ready,
                     args=(url, f"{url}healthz"), daemon=True).start()

    import uvicorn

    try:
        uvicorn.run("app.main:app", host=HOST, port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    print("\nCohandle stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
