"""DebugAssistAgent on localhost: give it a GitHub issue, watch the run, read the result.

  uv run debug-assist serve [--port=8777]     http://127.0.0.1:8777/            home: start a run, list of runs
                                              /run/<id>                          one run, live
                                              /replay/<id>?speed=8               a past run played back
                                              /connect                           connect a repo (connect.py), live
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
SESSION_S = 12 * 3600
_failed_logins: dict = {}   # client address → recent failed attempts (a public address gets 5 a minute)


def _public_host() -> str:
    """The hosted address (e.g. debugassist.onrender.com), set where it is deployed. Empty on the Mac."""
    import os  # Render sets RENDER_EXTERNAL_HOSTNAME itself, so a Render deploy needs no PUBLIC_HOST
    return (os.environ.get("PUBLIC_HOST") or os.environ.get("RENDER_EXTERNAL_HOSTNAME") or "").strip().lower()


def _password() -> str:
    import os
    return os.environ.get("APP_PASSWORD", "")


def _session_key() -> bytes:
    import hashlib
    return hashlib.sha256(b"da-session:" + _password().encode()).digest()


def make_session(now: float | None = None) -> str:
    import hmac
    exp = str(int((time.time() if now is None else now) + SESSION_S))
    return exp + "." + hmac.new(_session_key(), exp.encode(), "sha256").hexdigest()


def session_ok(value: str, now: float | None = None) -> bool:
    import hmac
    exp, _, sig = (value or "").partition(".")
    if not exp.isdigit() or int(exp) < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(sig, hmac.new(_session_key(), exp.encode(), "sha256").hexdigest())


def login_page(error: str = "") -> str:
    from . import icons
    e = viewer.e
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · {plain.NAME}</title><meta name="color-scheme" content="dark light"><link rel="stylesheet" href="/static/app.css">
</head><body><div class="ambient s-idle" aria-hidden="true"></div>
<main style="max-width:440px;width:100%"><header class="hero"><p class="eyebrow">{icons.mark(20)} {plain.NAME}</p><h1>Sign in</h1>
<p class="lede">This address can start runs that spend money, so it is locked.</p></header>
<form method="post" action="/login" class="group"><div class="sect"><div class="field">
<input name="password" type="password" autocomplete="current-password" placeholder="Password" aria-label="Password" required autofocus>
<button type="submit" class="btn glass prominent">Sign in</button></div></div>
<p class="err" role="alert">{e(error)}</p></form></main></body></html>"""
_plans: dict = {}
STATIC_FILES = {"app.css": "text/css; charset=utf-8", "topo.js": "text/javascript; charset=utf-8",
                "glass.js": "text/javascript; charset=utf-8", "chart.js": "text/javascript; charset=utf-8"}


def url(run_id: str | None = None, port: int = PORT) -> str:
    return f"http://127.0.0.1:{port}/" + (f"run/{run_id}" if run_id else "")


def _runs() -> list[str]:
    d = CFG.runs_dir
    from . import artifacts
    for rid in artifacts.run_ids():  # a host whose disk was wiped: the runs saved in MongoDB still list
        if RUN_ID.match(rid):
            (d / rid).mkdir(parents=True, exist_ok=True)
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
    from . import profiles
    if f"{owner}/{repo}" not in profiles.ready():
        raise Refused(f"{owner}/{repo} is not connected yet. Connect it first, from Connect a repo. "
                      f"Connected now: {', '.join(profiles.ready()) or 'none'}.")
    return profiles.get(f"{owner}/{repo}")


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


# ── connect a repo: one at a time, started the same way the terminal starts it ───────────────────────────────────
_connector: dict = {"proc": None, "repo": None}


def start_connect(link: str) -> str:
    from .connect import parse_repo
    from .profiles import ready
    try:
        repo = parse_repo(link)
    except ValueError as ex:
        raise Refused(str(ex))
    if repo in ready():
        raise Refused(f"{repo} is already connected. Paste one of its issues on the home page.")
    proc = _connector["proc"]
    if proc is not None and proc.poll() is None:
        raise Refused(f"{_connector['repo']} is being connected. Wait for it to finish, then connect another.")
    folder = CFG.runs_dir / "_connect"
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{repo.replace('/', '-')}.log", "w") as log:
        _connector["proc"] = subprocess.Popen([sys.executable, "-m", "debug_assist", "connect", f"https://github.com/{repo}"],
                                              cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                              start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    _connector["repo"] = repo
    return repo


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


STATE_ICON = {"Done": "done", "Waiting": "waiting", "Working": "running", "Interrupted": "waiting",
              "Stopped": "stopped", "Could not start": "stopped"}


CHECK_NAMES = {"Repo profile": "Repository set up", "Base checkout": "Code to test (vercel/ai)",
               "Sandbox (E2B)": "Test sandbox (E2B)", "Docker": "Test sandbox (Docker)", "MongoDB": "Database (MongoDB)",
               "Vector index": "Similar-bug search", "Embeddings": "Embeddings (Voyage)", "Ollama + Qwen3": "Embeddings (Ollama)",
               "Laya": "Laya (triage and guard rating)", "OpenRouter": "AI for writing tests and fixes (OpenRouter)",
               "GitHub token": "GitHub (read-only)", "Phoenix": "Tracing (Phoenix)", "Advisors": "Advisors"}


def checks_page() -> str:
    """Everything a run needs, green or red: the same start-up checks every run does (preflight.py), run now."""
    from . import icons
    from .preflight import run_preflight
    from .profiles import ready
    e = viewer.e
    t0 = time.monotonic()
    repo = (ready() or ["vercel/ai"])[0]
    checks = run_preflight(f"https://github.com/{repo}/issues/1")
    took = time.monotonic() - t0
    word = {"PASS": "Working", "WARN": "Working, with a note", "FAIL": "Not working"}
    cls = {"PASS": "done", "WARN": "waiting", "FAIL": "stopped"}
    icon = {"PASS": icons.check, "WARN": icons.pause, "FAIL": icons.cross}
    rows = "".join(
        f'<li class="row {cls[c.status]}"><span class="ic">{icon[c.status]()}</span><span class="t">'
        f'<b>{e(CHECK_NAMES.get(c.name, c.name))}</b><span>{e(word[c.status])}: {e(c.fact)}'
        + (f" · To fix: {e(c.fix)}" if c.fix and c.status != "PASS" else "") + "</span></span></li>" for c in checks)
    bad = sum(c.status == "FAIL" for c in checks)
    verdict = "Everything a run needs is working." if not bad else \
        f"{bad} thing{'s' * (bad != 1)} {'are' if bad != 1 else 'is'} not working. A run can't start until {'they are' if bad != 1 else 'it is'} fixed."
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>System check · {plain.NAME}</title><meta name="color-scheme" content="dark light"><link rel="stylesheet" href="/static/app.css">
</head><body>
<canvas id="topo" aria-hidden="true"></canvas><div class="ambient {'s-done' if not bad else 's-stopped'}" aria-hidden="true"></div>
<div class="scrim top" aria-hidden="true"></div>
<nav class="toolbar" aria-label="{plain.NAME}">
  <div class="tgroup glass"><a class="brand" href="/">{icons.mark()}<span>{plain.NAME}</span></a></div>
  <div class="tgroup glass"><a class="tbtn" href="/" aria-label="New run">{icons.plus(16)}<span class="lbl">New run</span></a>
    <a class="tbtn" href="/checks" aria-label="Check again">{icons.check(16)}<span class="lbl">Check again</span></a></div>
</nav>
<main>
<header class="hero"><h1>System check</h1>
  <p class="status {'s-done' if not bad else 's-stopped'}"><span class="dot"></span><span>{e(verdict)}</span></p>
  <p class="lede">These are the same checks every run does before it starts. Checked just now, in {took:.0f} s.</p></header>
<section class="group"><h2>Services</h2><div class="sect"><ul class="rows">{rows}</ul></div>
  <p class="foot">Each check makes one small request (the test sandbox starts and stops a sandbox, which uses a fraction of a cent of credit).</p></section>
</main>
<script src="/static/topo.js" defer></script><script src="/static/glass.js" defer></script>
</body></html>"""


def how_page() -> str:
    from . import chart, icons
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>How it works · {plain.NAME}</title><meta name="color-scheme" content="dark light">
<link rel="stylesheet" href="/static/app.css">
</head><body>
<canvas id="topo" aria-hidden="true"></canvas><div class="ambient s-idle" aria-hidden="true"></div>
<div class="scrim top" aria-hidden="true"></div>
<nav class="toolbar" aria-label="{plain.NAME}">
  <div class="tgroup glass"><a class="brand" href="/">{icons.mark()}<span>{plain.NAME}</span></a></div>
  <div class="tgroup glass"><a class="tbtn" href="/#runs" aria-label="Runs">{icons.list_(16)}<span class="lbl">Runs</span></a>
    <a class="tbtn" href="/" aria-label="New run">{icons.plus(16)}<span class="lbl">New run</span></a></div>
</nav>
<main>
<header class="hero"><h1>How it works</h1>
  <p class="lede">A run moves through these states, one action at a time. If an action fails, it tries again a fixed number of times, then stops and tells you why.</p></header>
<p class="legend"><span>Box: where the run is</span><span>Arrow: what it does next</span><span class="a">Amber: it tries again</span><span class="r">Red: it stops, with the reason</span><span class="b">Dot: the run</span></p>
{chart.section(None)}
</main>
<script src="/static/topo.js" defer></script><script src="/static/glass.js" defer></script><script src="/static/chart.js"></script>
</body></html>"""


CONNECT_STATE = {"done": ("done", "Done"), "running": ("running", "Working"), "waiting": ("pending", "Not started"),
                 "failed": ("stopped", "Stopped")}


def _connected_rows() -> str:
    from . import icons, profiles
    e, rows = viewer.e, []
    for repo in profiles.ready():
        try:
            p = profiles.get(repo)
        except Exception:
            continue
        passed = sum(r == "pass" for _, r in p.baseline)
        sub = (f"Connected {p.connected_at[:10]} · {p.language} · {p.manager} · {passed} of {len(p.baseline)} test suites pass"
               if p.source == "connected" else f"Set up by hand · {p.language} · {p.manager}")
        rows.append(f'<li class="row done"><span class="ic">{icons.check()}</span><span class="t"><b>{e(repo)}</b>'
                    f'<span>{e(sub)}</span></span></li>')
    return "".join(rows) or '<li class="row pending"><span class="ic"></span><span class="t"><span>None yet</span></span></li>'


def _connect_progress(repo: str) -> str:
    """The six steps of one connection, from MongoDB's `connects`; data-final once it has finished."""
    from . import icons
    from .connect import STEPS, status
    e = viewer.e
    doc = status(repo) if repo else None
    if not doc:
        return '<section hidden></section>'
    rows = []
    for s in doc.get("steps") or [{"key": k, "label": label, "status": "waiting"} for k, label in STEPS]:
        cls, word = CONNECT_STATE.get(s.get("status"), ("pending", s.get("status", "")))
        ic = icons.STATE[cls]() if cls in icons.STATE else icons.list_(18)
        rows.append(f'<li class="row {cls}"><span class="ic">{ic}</span><span class="t"><b>{e(s["label"])}</b>'
                    f'<span>{e(s.get("detail") or word)}</span></span></li>')
    st = doc.get("status")
    verdict = {"running": f"Connecting {repo}. It takes 10 to 20 minutes. You can leave this page.",
               "connected": f"Connected. Issues from {repo} can now be run.",
               "failed": f"Could not connect {repo}. {doc.get('why', '')}"}.get(st, st)
    cls = {"running": "s-live", "connected": "s-done", "failed": "s-stopped"}.get(st, "s-idle")
    log = "".join(f"<li>{e(x)}</li>" for x in (doc.get("log") or [])[-8:])
    final = ' data-final="1"' if st in ("connected", "failed") else ""
    return viewer._k("progress", f'<section class="group"{final}>'
            f'<h2>Connecting {e(repo)}</h2><p class="status {cls}"><span class="dot"></span><span>{e(verdict)}</span></p>'
            f'<div class="sect"><ul class="rows">{"".join(rows)}</ul></div>'
            + (f'<details class="foot"><summary>Latest output</summary><ul class="log">{log}</ul></details>' if log else "")
            + ('<p class="foot"><a href="/">Start a run</a></p>' if st == "connected" else "") + "</section>")


def connect_page(repo: str = "") -> str:
    from . import icons
    e = viewer.e
    if not repo and _connector["repo"]:
        repo = _connector["repo"]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Connect a repo · {plain.NAME}</title><meta name="color-scheme" content="dark light"><link rel="stylesheet" href="/static/app.css">
</head><body>
<canvas id="topo" aria-hidden="true"></canvas><div class="ambient s-idle" aria-hidden="true"></div>
<div class="scrim top" aria-hidden="true"></div>
<nav class="toolbar" aria-label="{plain.NAME}">
  <div class="tgroup glass"><a class="brand" href="/">{icons.mark()}<span>{plain.NAME}</span></a></div>
  <div class="tgroup glass"><a class="tbtn" href="/" aria-label="New run">{icons.plus(16)}<span class="lbl">New run</span></a>
    <a class="tbtn" href="/checks" aria-label="System check">{icons.check(16)}<span class="lbl">System check</span></a></div>
</nav>
<main>
<header class="hero"><h1>Connect a repo</h1>
  <p class="lede">Give it a GitHub repository. It works out how the repo installs and runs its tests, builds a test sandbox for it, and checks the tests pass there. After that, you can run any issue from that repo.</p></header>
<section class="group" aria-labelledby="h-repo"><h2 id="h-repo">GitHub repository</h2>
  <div class="sect"><div class="field"><input id="link" type="url" inputmode="url" placeholder="https://github.com/owner/repo" autocomplete="off" aria-label="GitHub repository link" value="{e(f'https://github.com/{repo}' if repo else '')}">
    <button type="button" class="btn glass prominent" id="go">Connect</button></div></div>
  <p class="foot">Public repos only, in JavaScript, TypeScript or Python. Building the test sandbox uses a few cents of E2B credit.</p>
  <p class="err" id="err" role="alert"></p></section>
{_connect_progress(repo)}
<section class="group" aria-labelledby="h-conn"><h2 id="h-conn">Connected repos</h2>
  <div class="sect"><ul class="rows">{_connected_rows()}</ul></div></section>
</main>
<script src="/static/topo.js" defer></script><script src="/static/glass.js" defer></script>
<script>
const H = {{ "{TOKEN_HEADER}": {json.dumps(TOKEN)}, "Content-Type": "application/json" }};
const $ = id => document.getElementById(id);
$("link").addEventListener("keydown", ev => {{ if (ev.key === "Enter") $("go").click(); }});
$("go").onclick = async () => {{
  $("err").textContent = ""; $("go").disabled = true;
  try {{
    const r = await fetch("/api/connect", {{ method: "POST", headers: H, body: JSON.stringify({{ url: $("link").value.trim() }}) }});
    const j = await r.json();
    if (!r.ok) {{ $("go").disabled = false; $("err").textContent = j.error || "Something went wrong."; return; }}
    location.href = "/connect?repo=" + encodeURIComponent(j.repo);
  }} catch (ex) {{ $("go").disabled = false; $("err").textContent = "Could not reach {plain.NAME}. Is it still running?"; }}
}};
async function poll() {{
  try {{
    const r = await fetch(location.href, {{ cache: "no-store" }});
    const doc = new DOMParser().parseFromString(await r.text(), "text/html");
    const n = doc.querySelector('[data-k="progress"]'), o = document.querySelector('[data-k="progress"]');
    if (n && o && n.dataset.h !== o.dataset.h) o.replaceWith(document.importNode(n, true));
    if (n && n.dataset.final === "1") return location.reload();
  }} catch (ex) {{}}
  setTimeout(poll, 3000);
}}
if (document.querySelector('[data-k="progress"]') && !document.querySelector('[data-final="1"]')) setTimeout(poll, 3000);
</script></body></html>"""


def home_page() -> str:
    from . import icons
    e = viewer.e
    rows = []
    for rid in _runs():
        got = _status_of(rid)
        if got is None:
            continue
        m = re.search(r"-(\d{8})-(\d{6})$", rid)
        when = datetime.strptime("".join(m.groups()), "%Y%m%d%H%M%S").strftime("%d %b, %H:%M") if m else ""
        issue, result = got
        st = next((v for k, v in STATE_ICON.items() if result.startswith(k)), "pending")
        ic = icons.STATE[st]() if st in icons.STATE else icons.mark(18)
        rows.append(f'<li><a class="row {st}" href="/run/{e(rid)}"><span class="ic">{ic}</span><span class="t">'
                    f'<b>{e(issue or re.sub(r"-[0-9]{8}-[0-9]{6}$", "", rid))}</b><span>{e(result)}</span></span>'
                    f'<span class="tr">{e(when)}{icons.chevron()}</span></a></li>')
        if len(rows) == 25:
            break
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{plain.NAME}</title><meta name="color-scheme" content="dark light">
<link rel="stylesheet" href="/static/app.css">
</head><body>
<canvas id="topo" aria-hidden="true"></canvas><div class="ambient s-idle" aria-hidden="true"></div>
<div class="scrim top" aria-hidden="true"></div>
<nav class="toolbar" aria-label="{plain.NAME}">
  <div class="tgroup glass"><a class="brand" href="/">{icons.mark()}<span>{plain.NAME}</span></a></div>
  <div class="tgroup glass"><a class="tbtn" href="#runs" aria-label="Runs">{icons.list_(16)}<span class="lbl">Runs</span></a>
    <a class="tbtn" href="/connect" aria-label="Connect a repo">{icons.plus(16)}<span class="lbl">Connect a repo</span></a>
    <a class="tbtn" href="/how" aria-label="How it works">{icons.play(16)}<span class="lbl">How it works</span></a>
    <a class="tbtn" href="/checks" aria-label="System check">{icons.check(16)}<span class="lbl">System check</span></a></div>
</nav>
<main>
<header class="hero">
  <h1>Start a run</h1>
  <p class="lede">Paste a GitHub issue. It collects context on the issue, reproduces the bug and fixes it for you. You review the fix and post it on GitHub.</p>
</header>
<section class="group" aria-labelledby="h-issue">
  <h2 id="h-issue">GitHub issue</h2>
  <div class="sect">
    <div class="field"><input id="link" type="url" inputmode="url" placeholder="https://github.com/owner/repo/issues/123" autocomplete="off" aria-label="GitHub issue link">
      <button type="button" class="btn glass" id="check">Check</button></div>
    <div class="issue-card" id="card" hidden><b id="ititle"></b><span id="imeta"></span></div>
  </div>
  <p class="err" id="err" role="alert"></p>
</section>
<div id="more" hidden>
  <section class="group" aria-labelledby="h-sec"><h2 id="h-sec">Which problem should it fix?</h2>
    <div class="sect" id="sections" role="radiogroup" aria-labelledby="h-sec"></div></section>
  <section class="group" aria-labelledby="h-ai" style="margin-top:28px"><h2 id="h-ai">Which AI?</h2>
    <div class="seg glass" role="radiogroup" aria-labelledby="h-ai">
      <label><input type="radio" name="ai" value="standard" checked>Standard<small>A few cents</small></label>
      <label><input type="radio" name="ai" value="opus">Claude Opus<small>About $0.70</small></label>
    </div>
    <p class="foot" id="aifoot">Cheaper. It often cannot fix the bug.</p></section>
  <div class="start" style="margin-top:22px"><button type="button" class="btn glass prominent" id="start">{icons.play(16)}<span>Start the run</span></button></div>
</div>
<section class="group" id="runs" aria-labelledby="h-runs"><h2 id="h-runs">Runs</h2>
  <div class="sect"><ul class="rows">{''.join(rows) or '<li class="row pending"><span class="ic"></span><span class="t"><span>No runs yet</span></span></li>'}</ul></div></section>
</main>
<script src="/static/topo.js" defer></script><script src="/static/glass.js" defer></script>
<script>
const TOKEN = {json.dumps(TOKEN)}, H = {{ "{TOKEN_HEADER}": TOKEN }};
const $ = id => document.getElementById(id);
const fail = msg => {{ $("err").textContent = msg; }};
const FOOT = {{ standard: "Cheaper. It often cannot fix the bug.", opus: "Stronger. About $0.70 a run, taken from your AI budget." }};
document.querySelectorAll('input[name="ai"]').forEach(r => r.addEventListener("change", () => {{ $("aifoot").textContent = FOOT[r.value]; }}));
$("link").addEventListener("keydown", ev => {{ if (ev.key === "Enter") $("check").click(); }});
$("check").onclick = async () => {{
  fail(""); $("more").hidden = true; $("card").hidden = true; $("check").disabled = true;
  try {{
    const r = await fetch("/api/issue?url=" + encodeURIComponent($("link").value.trim()), {{ headers: H }});
    const j = await r.json();
    if (!r.ok) return fail(j.error || "Something went wrong.");
    $("ititle").textContent = j.title;
    $("imeta").textContent = `${{j.owner}}/${{j.repo}} · Issue #${{j.number}} · ${{j.state === "open" ? "Open" : "Closed"}}`;
    $("card").hidden = false;
    const box = $("sections"); box.textContent = "";
    const add = (value, title, small, checked) => {{
      const l = document.createElement("label"); l.className = "choice";
      const i = document.createElement("input"); i.type = "radio"; i.name = "sec"; i.value = value; i.checked = checked;
      const t = document.createElement("span"); t.className = "t";
      const b = document.createElement("b"); b.textContent = title; t.appendChild(b);
      if (small) {{ const m = document.createElement("span"); m.textContent = small; t.appendChild(m); }}
      const tick = document.createElement("span"); tick.className = "tick"; tick.innerHTML = {json.dumps(icons.check())};
      l.append(i, t, tick); box.appendChild(l);
    }};
    add("", "The problem in the title", j.title, true);
    j.sections.forEach(x => add(x.heading, x.heading, x.preview, false));
    $("more").hidden = false;
  }} catch (ex) {{ fail("Could not reach {plain.NAME}. Is it still running?"); }}
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
  }} catch (ex) {{ $("start").disabled = false; fail("Could not reach {plain.NAME}. Is it still running?"); }}
}};
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "debug-assist-viewer"

    def log_message(self, *a):
        pass

    def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8", location: str | None = None,
              cookie: str | None = None):
        b = body.encode()
        self.send_response(code)
        if cookie:
            self.send_header("Set-Cookie", cookie)
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
        ok = {f"127.0.0.1:{port}", f"localhost:{port}"} | ({_public_host()} if _public_host() else set())
        return (self.headers.get("Host") or "").lower() in ok

    def _origin_ok(self) -> bool:
        port = self.server.server_address[1]
        ok = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"} | ({f"https://{_public_host()}"} if _public_host() else set())
        return self.headers.get("Origin") in ok

    def _signed_in(self) -> bool:
        """No password set (the Mac): always. A hosted address: only with a valid session cookie."""
        if not _password():
            return True
        from http.cookies import SimpleCookie
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return "da_session" in c and session_ok(c["da_session"].value)

    def _token_ok(self) -> bool:
        return secrets.compare_digest(self.headers.get(TOKEN_HEADER) or "", TOKEN)

    def do_GET(self):
        self.server.last = time.monotonic()
        if getattr(self.server, "locked", ""):  # a public address without its sign-in set: say why, serve nothing else
            if self.path == "/health":
                return self._send(200, HEALTH.decode(), "text/plain")
            return self._send(503, f"{plain.NAME} is locked. {self.server.locked}", "text/plain; charset=utf-8")
        if not self._host_ok():
            return self._send(403, "forbidden", "text/plain")
        u = urlparse(self.path)
        parts, q = [p for p in u.path.split("/") if p], parse_qs(u.query)
        try:
            if parts == ["health"]:
                return self._send(200, HEALTH.decode(), "text/plain")
            if parts == ["login"]:
                return self._send(200, login_page())
            if parts == ["static", "app.css"]:
                return self._send(200, (viewer.STATIC / "app.css").read_text(), STATIC_FILES["app.css"])
            if not self._signed_in():
                return self._send(302, "", "text/plain", location="/login")
            if not parts:
                return self._send(200, home_page())
            if parts == ["checks"]:
                return self._send(200, checks_page())
            if parts == ["how"]:
                return self._send(200, how_page())
            if parts == ["connect"]:
                from .connect import REPO
                repo = (q.get("repo") or [""])[0]
                return self._send(200, connect_page(repo if REPO.match(repo) else ""))
            if len(parts) == 2 and parts[0] == "static" and parts[1] in STATIC_FILES:
                return self._send(200, (viewer.STATIC / parts[1]).read_text(), STATIC_FILES[parts[1]])
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

    def _login(self):
        from urllib.parse import parse_qs as qs
        who = (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()
        now = time.time()
        recent = [t for t in _failed_logins.get(who, []) if now - t < 60]
        if len(recent) >= 5:
            return self._send(429, login_page("Too many tries. Wait a minute."))
        n = min(int(self.headers.get("Content-Length") or 0), 4096)
        given = (qs(self.rfile.read(n).decode(errors="replace")).get("password") or [""])[0]
        if not _password() or not secrets.compare_digest(given, _password()):
            _failed_logins[who] = recent + [now]
            return self._send(401, login_page("That password is not right."))
        secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") == "https" or _public_host()) else ""
        return self._send(302, "", "text/plain", location="/",
                          cookie=f"da_session={make_session()}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_S}{secure}")

    def do_POST(self):  # sign in, start a run, or connect a repo
        self.server.last = time.monotonic()
        if getattr(self.server, "locked", ""):
            return self._send(503, f"{plain.NAME} is locked. {self.server.locked}", "text/plain; charset=utf-8")
        if urlparse(self.path).path == "/login":
            if not self._host_ok() or (self.headers.get("Origin") and not self._origin_ok()):
                return self._send(403, "forbidden", "text/plain")
            return self._login()
        if (not self._host_ok() or not self._token_ok() or not self._origin_ok() or not self._signed_in()
                or not (self.headers.get("Content-Type") or "").startswith("application/json")):
            return self._json(403, {"error": "Not allowed. Start runs from the DebugAssistAgent home page."})
        path = urlparse(self.path).path
        if path not in ("/api/start", "/api/connect"):
            return self._json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4096:
            return self._json(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            if path == "/api/connect":
                repo = start_connect(str(body.get("url", "")))
                return self._json(200, {"repo": repo, "page": f"/connect?repo={repo}"})
            rid = start_run(str(body.get("url", "")), str(body.get("heading", "")), str(body.get("ai", "")))
            return self._json(200, {"run_id": rid, "page": f"/run/{rid}"})
        except Refused as r:
            return self._json(409 if "already going" in str(r) or "being connected" in str(r) else 400, {"error": str(r)})
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad request"})
        except Exception as ex:
            return self._json(500, {"error": f"Could not start ({type(ex).__name__})."})


def make(port: int = PORT, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.last = time.monotonic()
    return httpd


def locked_reason(host: str) -> str:
    """Why a public address must stay locked, in plain words; empty when it may open."""
    if host in ("127.0.0.1", "localhost"):
        return ""
    if len(_password()) < 12:
        return "APP_PASSWORD is missing or shorter than 12 characters. Set it in Render ▸ Environment, then redeploy."
    if not _public_host():
        return "The public address is unknown. Set PUBLIC_HOST in Render ▸ Environment, then redeploy."
    return ""


def serve(port: int = PORT, idle_s: int = IDLE_S, host: str = "127.0.0.1") -> None:
    why = locked_reason(host)
    print(f"{plain.NAME}: starting on {host}:{port}; public address {_public_host() or '(none)'}; "
          + (f"LOCKED: {why}" if why else ("sign-in required" if _password() else "no sign-in (this Mac only)")), flush=True)
    httpd = make(port, host)
    httpd.locked = why

    def watchdog():
        while True:
            time.sleep(min(30, idle_s))
            if time.monotonic() - httpd.last > idle_s:
                httpd.shutdown()
                return
    if idle_s:  # hosted (idle 0): the platform decides when it sleeps
        threading.Thread(target=watchdog, daemon=True).start()
    print(f"{plain.NAME}: http://{host}:{port}/" + (f" (stops after {idle_s // 60} min without a request)" if idle_s else ""), flush=True)
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
