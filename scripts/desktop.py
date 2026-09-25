"""Run Cohandle on this machine, as a desktop app.

Two ways in.

`run-cohandle.cmd` is the console version: you see the log, Ctrl+C stops
it. Good for when something is wrong.

The Start Menu shortcut runs this with `--windowed` through pythonw, so
there is no console at all. It opens in a chromeless browser window and
you stop it from the Quit button in the app's own header, because with no
console there is nothing to Ctrl+C.

Either way it is the same FastAPI app the deploy runs, served to
localhost only. Hosting costs nothing when the host is a laptop you
already own, and the intake worker runs for as long as it is open.

Bound to 127.0.0.1 deliberately. Anything else would put an app with no
login on the office network.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
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
LOG = ROOT / "cohandle-desktop.log"

# Chromeless window, so it reads as an application rather than a tab.
# Edge ships with Windows; Chrome is tried in case it is the default.
APP_BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def redirect_to_log() -> None:
    """Give the process a stdout, because pythonw does not.

    Under pythonw.exe sys.stdout and sys.stderr are None. Uvicorn logs to
    stdout, so the very first log line ends the process with an
    AttributeError that nobody can see. Pointing both at the log file
    fixes the crash and puts any traceback somewhere readable.
    """
    try:
        stream = LOG.open("a", encoding="utf-8", buffering=1)
    except OSError:
        return
    sys.stdout = sys.stderr = stream


def note(message: str, *, windowed: bool) -> None:
    """Say something, to wherever the user can actually see it."""
    stamp = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  " if windowed else ""
    try:
        print(f"{stamp}{message}", flush=True)
    except (OSError, ValueError, AttributeError):
        pass


def fatal(message: str, *, windowed: bool) -> int:
    note(message, windowed=windowed)
    if windowed:
        # No console to print to, so put it on the screen.
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None, message, "Cohandle", 0x10)
        except Exception:  # noqa: BLE001 - we are already failing
            pass
    return 1


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/healthz", timeout=1):
            return True
    except (urllib.error.URLError, OSError):
        return False


def pick_port() -> int:
    """The usual port, or any free one if something else has taken it."""
    for candidate in (PREFERRED_PORT, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((HOST, candidate))
                return sock.getsockname()[1]
            except OSError:
                continue
    raise SystemExit("no free port to listen on")


def show(url: str, *, windowed: bool) -> None:
    """Open the app window, falling back to an ordinary browser tab."""
    if windowed:
        for browser in APP_BROWSERS:
            if Path(browser).exists():
                subprocess.Popen([browser, f"--app={url}"])
                return
    webbrowser.open(url)


def open_when_ready(url: str, *, windowed: bool) -> None:
    """Open once the server answers, not a moment before.

    A fixed sleep would show an error page on a cold Neon compute, which
    takes a few seconds to wake.
    """
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}healthz", timeout=2) as response:
                if response.status == 200:
                    show(url, windowed=windowed)
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.3)
    note(f"server did not come up in time; try {url} yourself", windowed=windowed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Cohandle on this machine")
    parser.add_argument("--windowed", action="store_true",
                        help="no console: log to a file and open a chromeless window")
    args = parser.parse_args(argv)
    windowed = args.windowed
    if windowed:
        redirect_to_log()

    # One Cohandle is enough. A second shortcut click should bring the
    # window back, not start a rival server on another port.
    if already_running(PREFERRED_PORT):
        note("already running; opening the existing one", windowed=windowed)
        show(f"http://{HOST}:{PREFERRED_PORT}/", windowed=windowed)
        return 0

    try:
        from app.config import settings
    except ImportError as exc:
        return fatal(f"Dependencies are missing ({exc}).\n\n"
                     "Run this once, in the CoHandle folder:\n\n"
                     "    pip install -r requirements.txt", windowed=windowed)

    if not settings().database_url:
        return fatal("DATABASE_URL is not set, so there is no database to "
                     "talk to.\n\nCopy .env.example to .env and fill it in, "
                     "or run:\n\n    python scripts/setkey.py DATABASE_URL",
                     windowed=windowed)

    port = pick_port()
    url = f"http://{HOST}:{port}/"
    # Lets the app know it is on loopback: its no-login warning becomes
    # accurate, and the Quit button appears.
    os.environ["COHANDLE_LOOPBACK"] = "1"

    note(f"Cohandle is starting on {url}", windowed=windowed)
    if not windowed:
        print("Close this window, or press Ctrl+C, to stop it.\n", flush=True)

    threading.Thread(target=open_when_ready, args=(url,),
                     kwargs={"windowed": windowed}, daemon=True).start()

    import uvicorn

    from app.main import app as cohandle

    config = uvicorn.Config(cohandle, host=HOST, port=port,
                            log_level="warning", access_log=not windowed)
    server = uvicorn.Server(config)
    # The Quit button needs a handle on the server to stop it. Held on the
    # app rather than in a global so it cannot outlive the process.
    cohandle.state.server = server

    try:
        server.run()
    except KeyboardInterrupt:
        pass
    note("Cohandle stopped.", windowed=windowed)
    return 0


if __name__ == "__main__":
    windowed_run = "--windowed" in sys.argv
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - last line before silence
        import traceback

        note(traceback.format_exc(), windowed=windowed_run)
        raise SystemExit(fatal(
            f"Cohandle could not start.\n\n{exc}\n\n"
            f"Details are in {LOG.name}", windowed=windowed_run))
