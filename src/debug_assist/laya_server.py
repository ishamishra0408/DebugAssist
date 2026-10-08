"""Laya on the Mac, for hosted runs (Laya runs only on Apple chips). A tiny HTTP API that answers one thing:
POST /decide {text, questions, which} → {answers}, exactly what models.decide returns locally.

  LAYA_TOKEN=<a long random secret> uv run debug-assist laya-serve [--port=8790]
  then expose it with a tunnel (e.g. `cloudflared tunnel --url http://127.0.0.1:8790`) and set, where runs are hosted,
  LAYA_URL=<the tunnel's https address> and the same LAYA_TOKEN.

Refuses to start without LAYA_TOKEN. Listens on 127.0.0.1 only (the tunnel connects locally). Every request must carry
the token in the X-Laya-Token header (compared in constant time); bodies over 64 KB are refused. It decides; it reads
no files, runs no commands and holds no other keys.
"""
import json
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "laya"

    def log_message(self, *a):
        pass

    def _send(self, code: int, obj: dict):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not secrets.compare_digest(self.headers.get("X-Laya-Token") or "", self.server.token):
            return self._send(403, {"error": "forbidden"})
        if self.path != "/decide":
            return self._send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self._send(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(n))
            which = body.get("which", "general")
            if which not in ("general", "triage") or not isinstance(body.get("questions"), dict):
                return self._send(400, {"error": "bad request"})
            from .models import laya
            answers = laya(which).predict(str(body.get("text", "")), body["questions"])["answers"]
            return self._send(200, {"answers": answers})
        except (ValueError, KeyError):
            return self._send(400, {"error": "bad request"})
        except Exception as ex:
            return self._send(500, {"error": type(ex).__name__})


def make(port: int, token: str) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.token = token
    return httpd


def serve(port: int = 8790) -> None:
    token = os.environ.get("LAYA_TOKEN", "")
    if len(token) < 24:
        sys.exit("refusing to start: set LAYA_TOKEN to a long random secret (24+ characters) first")
    print(f"Laya API on http://127.0.0.1:{port} (token required); expose it with a tunnel", flush=True)
    make(port, token).serve_forever()
