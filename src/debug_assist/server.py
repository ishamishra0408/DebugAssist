"""DebugAssistAgent on localhost: give it a GitHub issue, watch the run, read the result.

  uv run debug-assist serve [--port=8777]     http://127.0.0.1:8777/            home: start a run, list of runs
                                              /run/<id>                          one run, live
                                              /replay/<id>?speed=8               a past run played back
`run` and `resume` start it (detached, if it isn't up) and open the run's page; --no-view skips that.

What the pages can do (ruled 2026-10-07): read everything, and start a run from a GitHub issue link (Isha asked for
an ingestion point). They cannot approve, reject or publish: those stay in the terminal. Starting a run spends money,
so the start button is locked to this page: it needs the token baked into the page, a same-origin POST with JSON, and
the Host header of this server (other web pages open in the browser can't press it). One run at a time. The server
listens on 127.0.0.1 only and stops after 30 minutes without a request.
"""
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import plain, viewer
from .config import CFG, ROOT

PORT = 8777
IDLE_S = 1800
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
ISSUE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/(\d+)/?$")
HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t#]*$", re.M)
HEALTH = b"debug-assist viewer"
TOKEN = secrets.token_urlsafe(24)  # new every time the server starts; only this server's pages carry it
TOKEN_HEADER = "X-DebugAssistAgent-Token"
_plans: dict = {}


def url(run_id: str | None = None, port: int = PORT) -> str:
    return f"http://127.0.0.1:{port}/" + (f"run/{run_id}" if run_id else "")


def _runs() -> list[str]:
    d = CFG.runs_dir
    if not d.is_dir():
        return []
    def started(n: str) -> str:  # the run id ends with its start time; other folders sort by when they changed
        m = re.search(r"(\d{8}-\d{6})$", n)
        return m.group(1) if m else datetime.fromtimestamp((d / n).stat().st_mtime).strftime("%Y%m%d-%H%M%S")
    return sorted((p.name for p in d.iterdir() if p.is_dir() and RUN_ID.match(p.name)), key=started, reverse=True)


# ── the issue, checked before anything starts ────────────────────────────────────────────────────────────────────
class Refused(Exception):
    """A plain-English reason the home page shows the operator."""


def _issue_parts(link: str) -> tuple[str, str, int]:
    m = ISSUE.match((link or "").strip())
    if not m:
        raise Refused("That is not a GitHub issue link. It should look like https://github.com/owner/repo/issues/123")
    return m.group(1), m.group(2), int(m.group(3))


def _ready_repo(owner: str, repo: str):
    from .checkout import check_base
    from .profiles import PROFILES
    prof = PROFILES.get(f"{owner}/{repo}")
    ready = [k for k, p in PROFILES.items() if check_base(p)[0]]
    if not prof or not check_base(prof)[0]:
        raise Refused(f"{owner}/{repo} is not set up yet. Repositories that are ready: {', '.join(ready) or 'none'}.")
    return prof


def read_issue(link: str) -> dict:
    """What the home page shows after "Check": the issue and the sections it could fix."""
    from .github_read import get_issue
    owner, repo, number = _issue_parts(link)
    _ready_repo(owner, repo)
    try:
        issue = get_issue(f"https://github.com/{owner}/{repo}/issues/{number}")
    except Exception as ex:
        raise Refused(f"Could not read the issue from GitHub ({type(ex).__name__}). Check the link and try again.")
    body = issue.get("body") or ""
    sections = []
    for h in dict.fromkeys(HEADING.findall(body)):  # each heading once, in order
        m = re.search(rf"^#+\s*{re.escape(h)}\s*#*\s*$\n(.*?)(?=^#+\s|\Z)", body, re.M | re.S)
        text = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        if text:
            sections.append({"heading": h, "preview": text[:180]})
    return {"owner": owner, "repo": repo, "number": number, "title": issue["title"], "state": issue["state"],
            "sections": sections}


# ── one run at a time, started the same way the terminal starts it ───────────────────────────────────────────────
_child: dict = {"proc": None, "run_id": None}


def start_run(link: str, heading: str, ai: str) -> str:
    owner, repo, number = _issue_parts(link)
    _ready_repo(owner, repo)
    if ai not in ("standard", "opus"):
        raise Refused("Pick which AI to use.")
    heading = (heading or "").strip()
    if heading and heading not in [s["heading"] for s in read_issue(link)["sections"]]:
        raise Refused("That section is not in the issue any more. Press Check again.")
    proc = _child["proc"]
    if proc is not None and proc.poll() is None:
        raise Refused(f"A run is already going ({_child['run_id']}). Wait for it to finish, then start another.")
    run_id = f"{repo}-{number}-{datetime.now():%Y%m%d-%H%M%S}"
    folder = CFG.runs_dir / run_id
    folder.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-m", "debug_assist", "run", f"https://github.com/{owner}/{repo}/issues/{number}",
            f"--run-id={run_id}", "--no-view"] + (["--demo"] if ai == "opus" else []) + \
           ([f"--focus-heading={heading}"] if heading else [])
    with open(folder / "console.log", "w") as log:
        _child["proc"] = subprocess.Popen(argv, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                          start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    _child["run_id"] = run_id
    return run_id


# ── pages ────────────────────────────────────────────────────────────────────────────────────────────────────────
def _status_of(run_id: str) -> tuple[str, str] | None:
    """(issue, result) for the run list; None for folders that are not runs (test scripts' trial folders)."""
    try:
        snap = viewer._app().get_state({"configurable": {"thread_id": run_id}})
    except Exception:
        return None
    v = snap.values or {}
    con = CFG.runs_dir / run_id / "console.log"
    if not v:
        if not con.exists():
            return None
        return "", ("Could not start" if "PREFLIGHT FAIL" in con.read_text(errors="replace") else "Getting ready")
    i = v.get("issue") or {}
    return f"{i.get('owner', '')}/{i.get('repo', '')} #{i.get('number', '')}" if i else "", _result(snap, v)


def _result(snap, v: dict) -> str:
    if (v.get("outcome") or {}).get("exit"):
        return plain.exit_text(v["outcome"]["exit"])
    if snap.tasks and snap.tasks[0].interrupts:
        return "Waiting for your OK"
    if not snap.next:
        return "Not running"
    step = plain.LABEL.get(snap.next[0], snap.next[0])
    age = (datetime.now().astimezone() - datetime.fromisoformat(snap.created_at)).total_seconds()
    return f"Interrupted at: {step}" if age > viewer.QUIET_S else f"Working: {step}"


def home_page() -> str:
    e = viewer.e
    rows = []
    for rid in _runs():
        got = _status_of(rid)
        if got is None:
            continue
        m = re.search(r"-(\d{8})-(\d{6})$", rid)
        when = datetime.strptime("".join(m.groups()), "%Y%m%d%H%M%S").strftime("%d %b %H:%M") if m else ""
        issue, result = got
        rows.append(f'<tr><td>{e(when)}</td><td>{e(issue or re.sub(r"-[0-9]{8}-[0-9]{6}$", "", rid))}</td><td>{e(result)}</td>'
                    f'<td><a href="/run/{e(rid)}">Open</a> · <a href="/replay/{e(rid)}">Replay</a></td></tr>')
        if len(rows) == 25:
            break
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{plain.NAME}</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{viewer._CSS}
.form {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:16px; display:grid; gap:14px }}
.form label.q {{ font-weight:600 }} .row {{ display:flex; gap:8px; flex-wrap:wrap }}
.row input {{ flex:1 1 320px; min-width:0; font:inherit; padding:8px 10px; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg) }}
.choice {{ display:flex; gap:8px; align-items:flex-start; padding:6px 0 }} .choice small {{ display:block; color:var(--muted) }}
button.go {{ font-size:15px; padding:8px 16px; background:var(--accent); color:#fff; border-color:var(--accent) }}
button:disabled {{ opacity:.5; cursor:default }} .err {{ color:var(--bad) }} #issue {{ display:none }}
</style></head><body><main>
<nav class="top"><b>{plain.NAME}</b></nav>
<section class="form" aria-label="Start a run">
  <h2>Start a run</h2>
  <div><label class="q" for="link">1. Paste the GitHub issue link</label>
    <div class="row"><input id="link" type="url" placeholder="https://github.com/owner/repo/issues/123" autocomplete="off">
    <button type="button" id="check">Check</button></div>
    <p class="err" id="err" role="alert"></p></div>
  <div id="issue">
    <p><b id="ititle"></b><br><span class="note" id="imeta"></span></p>
    <p class="q"><b>2. Which problem should it fix?</b></p><div id="sections"></div>
    <p class="q"><b>3. Which AI?</b></p>
    <label class="choice"><input type="radio" name="ai" value="standard" checked><span>Standard<small>A few cents a run. Weaker: it often cannot fix the bug.</small></span></label>
    <label class="choice"><input type="radio" name="ai" value="opus"><span>Claude Opus<small>About $0.70 a run. Stronger.</small></span></label>
    <p><button type="button" class="go" id="start">Start the run</button></p>
    <p class="note">It never posts anything to GitHub. When it finishes, you approve the result yourself, in your terminal.</p>
  </div>
</section>
<section><h2>Runs</h2><div class="scroll"><table><tr><th>Started</th><th>Issue</th><th>Result</th><th></th></tr>{''.join(rows) or '<tr><td colspan="4">No runs yet</td></tr>'}</table></div></section>
</main>
<script>
const TOKEN = {json.dumps(TOKEN)}, H = {{ "{TOKEN_HEADER}": TOKEN }};
const $ = id => document.getElementById(id);
const fail = msg => {{ $("err").textContent = msg; }};
$("check").onclick = async () => {{
  fail(""); $("issue").style.display = "none"; $("check").disabled = true;
  try {{
    const r = await fetch("/api/issue?url=" + encodeURIComponent($("link").value.trim()), {{ headers: H }});
    const j = await r.json();
    if (!r.ok) return fail(j.error || "Something went wrong.");
    $("ititle").textContent = j.title;
    $("imeta").textContent = `Issue #${{j.number}} in ${{j.owner}}/${{j.repo}} · ${{j.state === "open" ? "open" : "closed"}}`;
    const box = $("sections"); box.textContent = "";
    const add = (value, title, small, checked) => {{
      const l = document.createElement("label"); l.className = "choice";
      const i = document.createElement("input"); i.type = "radio"; i.name = "sec"; i.value = value; i.checked = checked;
      const s = document.createElement("span"); s.textContent = title;
      if (small) {{ const m = document.createElement("small"); m.textContent = small; s.appendChild(m); }}
      l.append(i, s); box.appendChild(l);
    }};
    add("", "The problem in the issue title", j.title, true);
    j.sections.forEach(x => add(x.heading, `The section "${{x.heading}}"`, x.preview + (x.preview.length >= 180 ? "…" : ""), false));
    $("issue").style.display = "block";
  }} catch (ex) {{ fail("Could not reach DebugAssistAgent. Is it still running?"); }}
  finally {{ $("check").disabled = false; }}
}};
$("start").onclick = async () => {{
  fail(""); $("start").disabled = true;
  const sec = document.querySelector('input[name="sec"]:checked'), ai = document.querySelector('input[name="ai"]:checked');
  try {{
    const r = await fetch("/api/start", {{ method: "POST", headers: {{ ...H, "Content-Type": "application/json" }},
      body: JSON.stringify({{ url: $("link").value.trim(), heading: sec ? sec.value : "", ai: ai ? ai.value : "" }}) }});
    const j = await r.json();
    if (!r.ok) {{ $("start").disabled = false; return fail(j.error || "Something went wrong."); }}
    location.href = j.page;
  }} catch (ex) {{ $("start").disabled = false; fail("Could not reach DebugAssistAgent. Is it still running?"); }}
}};
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "debug-assist-viewer"

    def log_message(self, *a):
        pass

    def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8", location: str | None = None):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Length", str(len(b)))
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(b)

    def _json(self, code: int, obj: dict):
        return self._send(code, json.dumps(obj), "application/json")

    def _host_ok(self) -> bool:  # a page from another site can't reach this server under its own name
        port = self.server.server_address[1]
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _token_ok(self) -> bool:
        return secrets.compare_digest(self.headers.get(TOKEN_HEADER) or "", TOKEN)

    def do_GET(self):
        self.server.last = time.monotonic()
        if not self._host_ok():
            return self._send(403, "forbidden", "text/plain")
        u = urlparse(self.path)
        parts, q = [p for p in u.path.split("/") if p], parse_qs(u.query)
        try:
            if not parts:
                return self._send(200, home_page())
            if parts == ["health"]:
                return self._send(200, HEALTH.decode(), "text/plain")
            if parts == ["api", "issue"]:
                if not self._token_ok():
                    return self._json(403, {"error": "This page is out of date. Reload it."})
                try:
                    return self._json(200, read_issue((q.get("url") or [""])[0]))
                except Refused as r:
                    return self._json(400, {"error": str(r)})
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

    def do_POST(self):  # one thing only: start a run
        self.server.last = time.monotonic()
        port = self.server.server_address[1]
        if (not self._host_ok() or not self._token_ok()
                or self.headers.get("Origin") not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
                or not (self.headers.get("Content-Type") or "").startswith("application/json")):
            return self._json(403, {"error": "Not allowed. Start runs from the DebugAssistAgent home page."})
        if urlparse(self.path).path != "/api/start":
            return self._json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4096:
            return self._json(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            rid = start_run(str(body.get("url", "")), str(body.get("heading", "")), str(body.get("ai", "")))
            return self._json(200, {"run_id": rid, "page": f"/run/{rid}"})
        except Refused as r:
            return self._json(409 if "already going" in str(r) else 400, {"error": str(r)})
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad request"})
        except Exception as ex:
            return self._json(500, {"error": f"Could not start the run ({type(ex).__name__})."})


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
    print(f"{plain.NAME}: {url(port=port)} (stops after {idle_s // 60} min without a request)", flush=True)
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
