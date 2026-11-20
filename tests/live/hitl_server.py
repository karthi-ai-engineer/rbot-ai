"""Serve the local HITL test pages (tests/live/hitl_pages) on http://127.0.0.1:8765.

  python tests/live/hitl_server.py            # runs until Ctrl+C
  python tests/live/hitl_server.py --port 9000

Importable:
  import hitl_server; base = hitl_server.start(); ...; hitl_server.stop()

Bound to 127.0.0.1 only (nothing is reachable from the network). Pages are sent with no-cache headers so every
run starts from a fresh page. If the port is already served by another instance, start() reuses it.
"""
import argparse
import functools
import http.server
import threading
import time
import urllib.request
from pathlib import Path

PAGES = Path(__file__).parent / "hitl_pages"
HOST, PORT = "127.0.0.1", 8765

_server = None
_thread = None


class _Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def log_message(self, fmt, *args):  # keep the agent's console readable
        pass


def base_url(port=PORT):
    return f"http://{HOST}:{port}/"


def url(page, port=PORT):
    return base_url(port) + page.lstrip("/")


def _alive(port):
    try:
        with urllib.request.urlopen(base_url(port) + "index.html", timeout=1) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def start(port=PORT):
    """Start serving in a daemon thread (idempotent). Returns the base URL."""
    global _server, _thread
    if _server is not None:
        return base_url(port)
    handler = functools.partial(_Handler, directory=str(PAGES))
    try:
        _server = http.server.ThreadingHTTPServer((HOST, port), handler)
    except OSError:
        if _alive(port):                 # another hitl_server is already running: just use it
            return base_url(port)
        raise
    _server.daemon_threads = True
    _thread = threading.Thread(target=_server.serve_forever, name="hitl_server", daemon=True)
    _thread.start()
    return base_url(port)


def stop():
    global _server, _thread
    if _server is not None:
        _server.shutdown()
        _server.server_close()
    _server, _thread = None, None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()
    base = start(a.port)
    print(f"Serving {PAGES} at {base}  (Ctrl+C to stop)")
    for p in sorted(PAGES.glob("*.html")):
        print(f"  {base}{p.name}")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop()
        print("stopped")


if __name__ == "__main__":
    main()
