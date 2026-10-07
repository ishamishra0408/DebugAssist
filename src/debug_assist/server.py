"""The run viewer on localhost: read-only pages that update in place while a run works.

  uv run debug-assist serve [--port=8777]     http://127.0.0.1:8777/            every run, newest first
                                              /run/<id>                          that run, live
                                              /replay/<id>?speed=8               a recorded run played back from its
                                                                                 checkpoints and event log
`run` and `resume` start it (detached, if it isn't up) and open the run's page; --no-view skips that.

GET only, bound to 127.0.0.1, no write path: the page can't approve, edit or run anything (approval stays in the
terminal, ruled 2026-10-07). It stops itself after 30 minutes without a request.
"""
import re
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import viewer
from .config import CFG, ROOT

PORT = 8777
IDLE_S = 1800
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
HEALTH = b"debug-assist viewer"
_plans: dict = {}


def url(run_id: str | None = None, port: int = PORT) -> str:
    return f"http://127.0.0.1:{port}/" + (f"run/{run_id}" if run_id else "")


def _runs() -> list[str]:
    d = CFG.runs_dir
    if not d.is_dir():
        return []
    return sorted((p.name for p in d.iterdir() if p.is_dir() and RUN_ID.match(p.name)),
                  key=lambda n: (d / n).stat().st_mtime, reverse=True)


def index_page() -> str:
    e = viewer.e
    rows = "".join(f'<li><a href="/run/{e(r)}">{e(r)}</a> · <a href="/replay/{e(r)}">replay</a></li>' for r in _runs())
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,'
            f'initial-scale=1"><title>Debug Assist Runs</title><style>:root{{--bg:#f6f7f9;--fg:#1f242c;--a:#2b5fae}}'
            f'@media (prefers-color-scheme:dark){{:root{{--bg:#0f1216;--fg:#e7eaef;--a:#7ea6ea}}}}'
            f'body{{background:var(--bg);color:var(--fg);font:15px/1.7 ui-monospace,monospace;margin:0;padding:24px 16px}}'
            f'a{{color:var(--a)}}ul{{padding-left:18px}}</style></head><body><h1 style="font:600 20px system-ui">Debug Assist runs'
            f'</h1><p>Newest first. Read-only.</p><ul>{rows or "<li>no runs yet</li>"}</ul></body></html>')


class Handler(BaseHTTPRequestHandler):
    server_version = "debug-assist-viewer"

    def log_message(self, *a):
        pass

    def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8", location: str | None = None):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):  # the only method: anything else gets the stdlib's 501
        self.server.last = time.monotonic()
        u = urlparse(self.path)
        parts, q = [p for p in u.path.split("/") if p], parse_qs(u.query)
        try:
            if not parts:
                return self._send(200, index_page())
            if parts == ["health"]:
                return self._send(200, HEALTH.decode(), "text/plain")
            if len(parts) == 2 and parts[0] in ("run", "replay"):
                rid = parts[1]
                if rid == "latest" and _runs():
                    return self._send(302, "", "text/plain", location=f"/{parts[0]}/{_runs()[0]}")
                if not RUN_ID.match(rid) or not (CFG.runs_dir / rid).is_dir():
                    return self._send(404, "no such run", "text/plain")
                if parts[0] == "run":
                    return self._send(200, viewer.render(viewer.gather(rid), mode="live"))
                speed = min(60.0, max(1.0, float((q.get("speed") or ["8"])[0])))
                plan = _plans.get((rid, speed)) or _plans.setdefault((rid, speed), viewer.replay_plan(rid, speed))
                if not plan:
                    return self._send(404, "nothing recorded for this run", "text/plain")
                elapsed = max(0.0, float((q.get("ms") or ["0"])[0]) / 1000)
                d = viewer.gather(rid, at=viewer.real_at(plan, elapsed))
                d["replay"] = True
                return self._send(200, viewer.render(d, mode="replay", replay={
                    "speed": speed, "elapsed": min(elapsed, plan[-1][0]), "length": plan[-1][0],
                    "final": elapsed >= plan[-1][0]}))
            return self._send(404, "not found", "text/plain")
        except ValueError:
            return self._send(400, "bad request", "text/plain")
        except Exception as ex:  # never a traceback (with paths) in the page
            return self._send(500, f"viewer error: {type(ex).__name__}", "text/plain")


def make(port: int = PORT) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.last = time.monotonic()
    return httpd


def serve(port: int = PORT, idle_s: int = IDLE_S) -> None:
    httpd = make(port)

    def watchdog():
        while True:
            time.sleep(min(30, idle_s))
            if time.monotonic() - httpd.last > idle_s:
                httpd.shutdown()
                return
    threading.Thread(target=watchdog, daemon=True).start()
    print(f"run viewer: {url(port=port)} (read-only; stops after {idle_s // 60} min without a request)", flush=True)
    httpd.serve_forever()


def is_up(port: int = PORT) -> bool:
    try:
        with urllib.request.urlopen(url(port=port) + "health", timeout=1) as r:
            return r.read() == HEALTH
    except OSError:
        return False


def ensure(port: int = PORT) -> bool:
    """Start the viewer in its own process (it outlives the run, so the page still answers after a pause or stop)."""
    if is_up(port):
        return True
    CFG.runs_dir.mkdir(parents=True, exist_ok=True)
    with open(CFG.runs_dir / ".viewer.log", "a") as log:
        subprocess.Popen([sys.executable, "-m", "debug_assist", "serve", f"--port={port}"], cwd=ROOT,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(50):
        if is_up(port):
            return True
        time.sleep(0.2)
    return False
