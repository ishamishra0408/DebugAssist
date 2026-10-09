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
from pathlib import Path
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
def _public_host() -> str:
    """The hosted address (e.g. debugassist.onrender.com), set where it is deployed. Empty on the Mac."""
    import os  # Render sets RENDER_EXTERNAL_HOSTNAME itself, so a Render deploy needs no PUBLIC_HOST
    return (os.environ.get("PUBLIC_HOST") or os.environ.get("RENDER_EXTERNAL_HOSTNAME") or "").strip().lower()


LOGIN_ERRORS = {"state": "Sign-in timed out. Try again.", "github": "Couldn't reach GitHub. Try again.",
                "cancelled": "Sign-in was canceled.", "off": "Sign-in isn't set up yet."}
GITHUB_MARK = ('<svg width="20" height="20" viewBox="0 0 16 16" aria-hidden="true" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 '
               '8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94'
               '-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64'
               '-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 '
               '2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73'
               '.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>')


AUTH_FLOW = ("read_issue", "reproduce", "find_cause", "write_fix", "approval")   # the brand side's preview of a run


def login_page(error: str = "", refused: str = "") -> str:
    """Sign in (Isha 2026-10-08: GitHub's wording, the 21st.dev sign-in patterns, the app's Liquid Glass): a split glass
    card on the app's living background; the brand side says what it does and previews a run's steps lighting in turn
    (illustration only: nothing about real runs shows before sign-in); the form side has one button."""
    from . import icons
    e = viewer.e
    msg = f"{refused} doesn't have access." if refused else LOGIN_ERRORS.get(error, "")
    flash = f'<p class="auth-flash" role="alert">{e(msg)}</p>' if msg else ""
    steps = {k: (label, gives) for k, label, gives in plain.STEPS}
    flow = "".join(f'<li style="--i:{i}"><span class="d"></span><span class="t"><b>{e(steps[k][0])}</b>'
                   f'<small>{e(steps[k][1])}</small></span></li>' for i, k in enumerate(AUTH_FLOW))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Sign in to {plain.NAME}</title><meta name="color-scheme" content="dark light"><link rel="stylesheet" href="/static/app.css">
</head><body class="auth">
<canvas id="topo" aria-hidden="true"></canvas><div class="ambient s-idle" aria-hidden="true"></div>
<main class="auth-wrap"><div class="auth-split glass">
<section class="auth-brand" aria-label="What {plain.NAME} does">
  <p class="auth-logo">{icons.mark(20)}<span>{plain.NAME}</span></p>
  <h2>From a GitHub issue to a proven fix.</h2>
  <ol class="auth-flow">{flow}</ol>
  <p class="auth-proof">Every fix passes 2 tests before you see it.</p>
</section>
<section class="auth-form">
  <div class="auth-mark">{icons.mark(26)}</div>
  <h1>Sign in to {plain.NAME}</h1>
  <p class="auth-sub">Use your GitHub account to continue.</p>
  {flash}<a class="auth-btn" href="/auth/github">{GITHUB_MARK}<span>Continue with GitHub</span></a>
  <p class="auth-foot">New here? Ask the owner to add your GitHub account.</p>
</section>
</div></main>
<script src="/static/topo.js" defer></script><script src="/static/glass.js" defer></script>
</body></html>"""


_plans: dict = {}
STATIC_FILES = {"app.css": "text/css; charset=utf-8", "topo.js": "text/javascript; charset=utf-8",
                "glass.js": "text/javascript; charset=utf-8", "chart.js": "text/javascript; charset=utf-8",
                "advisors.js": "text/javascript; charset=utf-8"}


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


# headings that are parts of one report (an issue template), and words that mark a second, separate problem
PART = re.compile(r"description|summary|reproduc|steps|expected|actual|current|behaviou?r|what happened|environment|"
                  r"version|system|setup|code|example|logs?|error|stack|trace|screenshot|context|notes?|related|"
                  r"proposed|possible|solution|fix|workaround|impact|details|output|bug", re.I)
ANOTHER = re.compile(r"\b(second(ary)?|another|also|other|separate|additional (bug|issue|problem)|"
                     r"(bug|issue|problem)\s*#?\s*[2-9]|follow[- ]up)\b", re.I)


def pick_focus(title: str, sections: list[dict], texts: dict) -> dict:
    """Which one problem to prove, picked from the issue's own headings (Isha 2026-10-08: don't ask when the issue
    holds one problem). single: every heading is part of one report. The pick: the first section that describes the
    problem and quotes code (its backticks are what the test's failure must show), else the title."""
    single = not any(ANOTHER.search(s["heading"]) or not PART.search(s["heading"]) for s in sections)
    describes = [s for s in sections if re.search(r"description|actual|current|what happened|summary|behaviou?r|bug|error",
                                                   s["heading"], re.I) and not re.search(r"reproduc|steps|expected", s["heading"], re.I)]
    coded = [s for s in describes if "`" in texts.get(s["heading"], "") and not texts[s["heading"]].lstrip().startswith("```")]
    best = (coded or [None])[0]
    return {"single": single, "heading": best["heading"] if best else "",
            "preview": best["preview"] if best else title, "from": f"the {best['heading']} section" if best else "the title"}


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
    sections, texts = [], {}
    for h in dict.fromkeys(HEADING.findall(body)):  # each heading once, in order
        m = re.search(rf"^#+\s*{re.escape(h)}\s*#*\s*$\n(.*?)(?=^#+\s|\Z)", body, re.M | re.S)
        text = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        if text:
            sections.append({"heading": h, "preview": text[:180]})
            texts[h] = m.group(1)
    return {"owner": owner, "repo": repo, "number": number, "title": issue["title"], "state": issue["state"],
            "auto": pick_focus(issue["title"], sections, texts),
            "sections": sections}


# ── one run at a time, started the same way the terminal starts it ───────────────────────────────────────────────
_child: dict = {"proc": None, "run_id": None}
_start_lock = threading.Lock()   # the home page and the automatic worker never start two runs at once


def run_going() -> bool:
    return _child["proc"] is not None and _child["proc"].poll() is None


_PATH = re.compile(r"[\w@+-][\w@.+-]*(?:/[\w@.+-]+)*")
_BLOB = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/blob/[^/]+/(.+?)(?:[#?].*)?$")


def pointers(look_in: str, test_in: str) -> tuple[list[str], str]:
    """The person's optional pointers (Isha 2026-10-08), checked: paths from the repo's top folder (or GitHub file
    links), no "..", at most 5 files to look in, one test file. Whether they exist is checked in the run's own copy."""
    from .lang import is_test_path

    def clean(raw: str) -> str:
        raw = raw.strip().strip("`'\"")
        m = _BLOB.match(raw)
        raw = (m.group(1) if m else raw).removeprefix("./")
        if not raw:
            return ""
        if len(raw) > 200 or ".." in raw.split("/") or not _PATH.fullmatch(raw):
            raise Refused(f"{raw[:80]} is not a path in the repo. Write it from the repo's top folder, e.g. src/app.ts.")
        return raw
    files = [c for c in (clean(x) for x in re.split(r"[,\s]+", look_in or "")) if c]
    if len(files) > 5:
        raise Refused("Point to at most 5 files for the cause.")
    for f in files:
        if is_test_path(f):
            raise Refused(f"{f} is a test file. The cause is looked for in the code itself; put a test file in "
                          "Write the unit test in.")
    test = clean(test_in or "")
    if test and not is_test_path(test):
        raise Refused(f"{test} is not a test file by its name (e.g. chat.test.ts, test_chat.py).")
    return list(dict.fromkeys(files)), test


def start_run(link: str, heading: str, ai: str, look_in: str = "", test_in: str = "") -> str:
    with _start_lock:
        return _start_run(link, heading, ai, look_in, test_in)


def _start_run(link: str, heading: str, ai: str, look_in: str = "", test_in: str = "") -> str:
    owner, repo, number = _issue_parts(link)
    files, test = pointers(look_in, test_in)
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
           ([f"--focus-heading={heading}"] if heading else []) + \
           ([f"--look-in={','.join(files)}"] if files else []) + ([f"--test-in={test}"] if test else [])
    with open(folder / "console.log", "w") as log:
        _child["proc"] = subprocess.Popen(argv, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                          start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    _child["run_id"] = run_id
    return run_id


# ── your OK, from the page (Isha 2026-10-08): the same command as the terminal, bound to the text shown ──────────
_decider: dict = {"proc": None, "run_id": None}


def decide(run_id: str, decision: str, sha: str, commit_message: str = "", by: str = "") -> str:
    """Approve or say no to a run waiting for your OK. Refused unless the run is waiting, and the text on disk is the
    text you were shown (its sha256, sent by the page). Runs `debug-assist approve|reject <run-id>` in the background."""
    from .guardrails import fingerprint
    if decision not in ("approve", "reject"):
        raise Refused("Choose approve or say no.")
    if not RUN_ID.match(run_id) or not (CFG.runs_dir / run_id).is_dir():
        raise Refused("No such run.")
    proc = _decider["proc"]
    if proc is not None and proc.poll() is None:
        raise Refused("Your last answer is still being saved. Wait a few seconds.")
    snap = viewer._app().get_state({"configurable": {"thread_id": run_id}})
    intr = next((i.value for t in (snap.tasks or []) for i in (t.interrupts or [])), None)
    if not intr:
        raise Refused("This run is not waiting for your OK.")
    if sha != intr.get("sha256"):
        raise Refused("The pull request text changed since this page was opened. Reload it and read it again.")
    from . import artifacts
    if artifacts.enabled():
        artifacts.restore_run(run_id)
    pr = Path(intr["pr_body_path"])
    if not pr.exists() or fingerprint(pr.read_text()) != sha:
        raise Refused("The pull request text on disk is not the text you were shown. Nothing was approved.")
    msg = commit_message.replace("\r\n", "\n").strip()
    if decision == "approve" and msg:
        if len(msg) > 10000 or not msg.splitlines()[0].strip():
            raise Refused("The commit message needs a first line (the title), and fewer than 10,000 characters.")
        mp = Path(intr.get("commit_message_path") or CFG.runs_dir / run_id / "commit-message.txt")
        mp.write_text(msg + "\n")   # yours: the commit carries it; its fingerprint is logged with the approval
        from . import events
        with events.bind(run_id, "approval"):
            events.log("commit_message", key="commit", sha256=fingerprint(msg + "\n"), title=msg.splitlines()[0][:120])
    from . import events
    with events.bind(run_id, "approval"):   # who answered, from their GitHub sign-in ("" on this Mac without sign-in)
        events.log("decision", key=f"decision-{sha[:12]}", decision=decision, by=by, sha256=sha)
    with open(CFG.runs_dir / run_id / "console-decision.log", "w") as log:
        _decider["proc"] = subprocess.Popen([sys.executable, "-m", "debug_assist", decision, run_id, "--no-view"], cwd=ROOT,
                                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                            start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    _decider["run_id"] = run_id
    return ("Approved. Saving it now; this page updates in a few seconds." if decision == "approve"
            else "You said no. Ending the run; this page updates in a few seconds.")


# ── connect a repo: one at a time, started the same way the terminal starts it ───────────────────────────────────
_connector: dict = {"proc": None, "repo": None}


def _database_problem() -> str:
    """'' when MongoDB answers; otherwise why, in plain words. A connection saves every step there (and the page
    reads them from there), so it must not start without it (2026-10-08: it crashed unseen, the page stayed blank)."""
    from .config import CFG as cfg
    try:
        from .store import client
        client(1500).admin.command("ping")
        return ""
    except Exception:
        local = any(h in (cfg.mongodb_uri or "") for h in ("127.0.0.1", "localhost"))
        return ("The database is not reachable, so the connection could not be saved. " +
                ("On this Mac the database runs in Docker Desktop: open Docker Desktop, wait a minute, then try again."
                 if local else "Check MONGODB_URI in the host's settings."))


def start_connect(link: str, again: bool = False) -> str:
    """again: connect a repo that is already connected, at its latest code (a new test sandbox; the old setup keeps
    working until the new one is saved)."""
    from .connect import parse_repo
    from .profiles import PROFILES, ready
    try:
        repo = parse_repo(link)
    except ValueError as ex:
        raise Refused(str(ex))
    if repo in PROFILES and PROFILES[repo].base_commit:
        raise Refused(f"{repo} is already set up. Paste one of its issues on the home page.")
    if repo in ready() and not again:
        raise Refused(f"{repo} is already connected. Paste one of its issues on the home page.")
    proc = _connector["proc"]
    if proc is not None and proc.poll() is None:
        raise Refused(f"{_connector['repo']} is being connected. Wait for it to finish, then connect another.")
    problem = _database_problem()
    if problem:
        raise Refused(problem)
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
<p class="legend"><span>Box: where the run is</span><span>Arrow: what it does next</span><span class="a">Amber: it tries again</span><span class="r">Red: it stops, with the reason</span><span class="b">Dot: the run</span><span class="g">Advisor: reviews that step, advice only</span></p>
{chart.section(None)}
</main>
<script src="/static/topo.js" defer></script><script src="/static/glass.js" defer></script><script src="/static/chart.js"></script>
</body></html>"""


def _connect_started(repo: str) -> str:
    """Before the connection has saved its first step: starting, or it ended without saving one (the reason is the last
    line it printed)."""
    proc = _connector["proc"]
    if not repo or _connector["repo"] != repo or proc is None:
        return '<section hidden></section>'
    e = viewer.e
    if proc.poll() is None:
        body = (f'<section class="group"><h2>Connecting {e(repo)}</h2><p class="status s-live"><span class="dot"></span>'
                '<span>Starting.</span></p></section>')
    else:
        log = CFG.runs_dir / "_connect" / f"{repo.replace('/', '-')}.log"
        lines = [x for x in (log.read_text(errors="replace").splitlines() if log.exists() else []) if x.strip()]
        why = lines[-1][:300] if lines else "it stopped before saving anything"
        body = (f'<section class="group" data-final="1"><h2>Connecting {e(repo)}</h2><p class="status s-stopped">'
                f'<span class="dot"></span><span>Could not connect {e(repo)}. {e(why)}</span></p></section>')
    return viewer._k("progress", body)


CONNECT_STATE = {"done": ("done", "Done"), "running": ("running", "Working"), "waiting": ("pending", "Not started"),
                 "failed": ("stopped", "Stopped")}


def _connected_rows() -> str:
    from . import icons, profiles
    from .connect import recent
    e, rows = viewer.e, []
    tried = {d["_id"]: d for d in recent()}
    for repo, d in tried.items():  # under way, or tried and not connected: shown first, with where it stands
        if d.get("status") == "connected" or repo in profiles.ready():
            continue
        if d.get("status") == "running":
            now = next((x["label"] for x in d.get("steps", []) if x.get("status") == "running"), "Starting")
            cls, ic, sub = "running", icons.spinner(), f"Connecting now: {now}"
        else:
            cls, ic, sub = "stopped", icons.cross(), f"Not connected. {d.get('why', '')}"
        rows.append(f'<li><a class="row {cls}" href="/connect?repo={e(repo)}"><span class="ic">{ic}</span><span class="t">'
                    f'<b>{e(repo)}</b><span>{e(sub)}</span></span><span class="tr">{icons.chevron()}</span></a></li>')
    for repo in profiles.ready():
        try:
            p = profiles.get(repo)
        except Exception:
            continue
        passed = sum(r == "pass" for _, r in p.baseline)
        sub = (f"Connected {p.connected_at[:10]} · {p.language} · {p.manager} · {passed} of {len(p.baseline)} test suites pass"
               if p.source == "connected" else f"Set up by hand · {p.language} · {p.manager}")
        if (tried.get(repo) or {}).get("status") == "running":
            sub += " · connecting again now"
        again = (f'<button type="button" class="btn glass again" data-repo="{e(repo)}">Connect again</button>'
                 if p.source == "connected" else "")
        rows.append(f'<li class="row done"><span class="ic">{icons.check()}</span><span class="t"><b>{e(repo)}</b>'
                    f'<span>{e(sub)}</span></span>{again}</li>')
    return "".join(rows) or '<li class="row pending"><span class="ic"></span><span class="t"><span>None yet</span></span></li>'


def _connect_progress(repo: str) -> str:
    """The six steps of one connection, from MongoDB's `connects`; data-final once it has finished."""
    from . import icons
    from .connect import STEPS, status
    e = viewer.e
    doc = status(repo) if repo else None
    if not doc:
        return _connect_started(repo)
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


def _advisor_cards() -> str:
    """One card per advisor seat (advisors.REVIEWS): which step it reviews, what it checks, whether it is on; then the
    steps to connect them, each ticked on its own facts (Isha 2026-10-08). Nothing here switches them on: the server
    is reviewed first, then three settings are set by hand where DebugAssistAgent runs. The page never claims more than
    is true: no key means the server will refuse, and it says so."""
    import os

    from . import advisors, icons
    from .config import CFG as cfg
    e = viewer.e
    st, _ = advisors.status()
    has_key = bool(os.environ.get("ADVISORS_KEY", "").strip())
    live = st == "ON" and advisors.CALL_WRITTEN and has_key
    label = {k: lab for k, lab, _ in plain.STEPS}
    num = {k: i + 1 for i, (k, _, _) in enumerate(plain.STEPS)}
    pill = (("On", "green") if live else ("No key yet", "amber") if st == "ON" else
            ("Waiting for review", "amber") if st == "BLOCKED" else ("Off", "grey"))
    def ask_box(seat: str) -> str:
        a = advisors.ASK[seat]
        boxes = "".join(
            f'<label><span>{e(label)}</span>' + (f'<textarea name="{name}" rows="3" placeholder="{e(hint)}"></textarea>' if kind == "textarea"
                                                 else f'<input type="text" name="{name}" placeholder="{e(hint)}" autocomplete="off">') + '</label>'
            for name, label, hint, kind in a["fields"])
        return (f'<div class="adv-ask" data-seat="{e(seat)}"><h4>Ask {e(seat)} yourself</h4>'
                f'<p class="hint">{e(a["useful"])}</p>{boxes}'
                f'<div class="adv-go"><button type="button" class="btn glass prominent">Ask {e(seat)}</button>'
                f'<span class="adv-note">Takes up to a minute if their server is asleep.</span></div>'
                f'<div class="adv-answer" hidden></div></div>')

    seats = [(step, r, advisors.ASK.get(r["seat"], {})) for step, r in advisors.REVIEWS.items()]
    at_step = {num[step]: (r["seat"], who.get("palette", "tide")) for step, r, who in seats}
    track = "".join(
        f'<li class="{"adv-at" if i + 1 in at_step else ""}">'
        + (f'<button type="button" class="adv-pick mark pal-{e(at_step[i + 1][1])}" data-seat="{e(at_step[i + 1][0])}" '
           f'aria-label="{e(at_step[i + 1][0])}, step {i + 1}"></button>' if i + 1 in at_step else '<span class="tick"></span>')
        + f'<span class="n">{i + 1}</span><span class="lab">{e(lab)}</span>'
        + (f'<span class="who">{e(at_step[i + 1][0])}</span>' if i + 1 in at_step else "") + '</li>'
        for i, (_, lab, _) in enumerate(plain.STEPS))
    tiles = "".join(
        f'<button type="button" class="adv-tile adv-pick{" on" if k == 0 else ""}" role="tab" aria-selected="{str(k == 0).lower()}" '
        f'data-seat="{e(r["seat"])}" aria-controls="adv-panel-{e(r["seat"])}">'
        f'<canvas class="sigil" data-seat="{e(r["seat"])}" data-palette="{e(who.get("palette", "tide"))}" aria-hidden="true"></canvas>'
        f'<span class="adv-step">Step {num[step]}</span><span class="adv-dot {pill[1]}" title="{e(pill[0])}"></span>'
        f'<span class="adv-tile-id"><span class="adv-role">{e(who.get("role", "Advisor"))}</span><b>{e(r["seat"])}</b></span></button>'
        for k, (step, r, who) in enumerate(seats))
    panels = "".join(
        f'<div class="adv-panel" id="adv-panel-{e(r["seat"])}" role="tabpanel" data-seat="{e(r["seat"])}"{"" if k == 0 else " hidden"}>'
        f'<div class="adv-panel-head"><p class="adv-motto">\u201c{e(who.get("motto", ""))}\u201d</p>'
        f'<span class="pill {pill[1]}">{e(pill[0])}</span></div>'
        f'<dl class="adv-facts"><div><dt>Asked</dt><dd>After step {num[step]}, {e(label[step])}. Advice only.</dd></div>'
        f'<div><dt>Checks</dt><dd>{e(r["question"])}</dd></div>'
        f'<div><dt>Gets</dt><dd>{e(who.get("gets", ""))}</dd></div></dl>'
        + (ask_box(r["seat"]) if live and r["seat"] in advisors.ASK else "") + '</div>'
        for k, (step, r, who) in enumerate(seats))
    cards = (f'<ol class="adv-track" aria-label="Where in a run each advisor is asked">{track}</ol>'
             f'<div class="adv-rail" role="tablist" aria-label="Advisors">{tiles}</div>{panels}')
    steps = [("Server address", bool(cfg.advisors_mcp),
              f"Set: {cfg.advisors_mcp}" if cfg.advisors_mcp else
              "ADVISORS_MCP = https://domain-expertise-mcp.onrender.com/mcp/ (from the advisors' team)"),
             ("Review the server", bool(cfg.advisors_reviewed),
              "Reviewed, and marked so." if cfg.advisors_reviewed else
              "Reviewed on 8 Oct 2026 (its lock, what it keeps, what it answers). Set ADVISORS_REVIEWED = yes to accept it."),
             ("Add the key", has_key, "Set." if has_key else
              "ADVISORS_KEY = the key the advisors' team sends you (Render: Environment). Never in chat."),
             ("The question call", advisors.CALL_WRITTEN,
              "Written: each review point asks the server's advise tool; answers are saved with the run." if advisors.CALL_WRITTEN
              else "Written against the reviewed server's tools.")]
    first = next((i for i, (_, ok, _) in enumerate(steps) if not ok), None)
    rows = "".join(
        f'<li class="row {"done" if ok else "next" if i == first else "pending"}"><span class="ic">'
        f'{icons.check() if ok else f"<span class=num>{i + 1}</span>"}</span><span class="t"><b>{e(t)}</b><span>{e(d)}</span></span></li>'
        for i, (t, ok, d) in enumerate(steps))
    state = ("Connected. Each advisor is asked during every run, at its step." if live else
             "Switched on, but no key is set, so the server will refuse." if st == "ON" and advisors.CALL_WRITTEN else
             "Switched on, but the question call is not written yet, so nobody is asked." if st == "ON" else
             "An address is set, but the server has not been marked reviewed, so the advisors stay off." if st == "BLOCKED" else
             "Not connected. Three settings switch them on: the steps below.")
    return (f'<section class="group" id="advisors" aria-labelledby="h-adv"><h2 id="h-adv">Advisors</h2>'
            f'<p class="status {"s-done" if live else "s-waiting" if st in ("BLOCKED", "ON") else "s-idle"}">'
            f'<span class="dot"></span><span>{e(state)}</span></p>'
            f'<div class="adv-cards">{cards}</div>'
            f'<details class="adv-connect"{" open" if first is not None else ""}><summary>How to connect them · '
            f'{sum(ok for _, ok, _ in steps)} of {len(steps)} done</summary><div class="sect"><ul class="rows">{rows}</ul></div></details>'
            '<p class="foot">Advice only. An advisor never changes the fix, the pull request text or your OK.</p></section>')


def connect_page(repo: str = "") -> str:
    from . import icons
    e = viewer.e
    if not repo and _connector["repo"]:
        repo = _connector["repo"]
    rows = _connected_rows()
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Connect · {plain.NAME}</title><meta name="color-scheme" content="dark light"><link rel="stylesheet" href="/static/app.css">
</head><body>
<canvas id="topo" aria-hidden="true"></canvas><div class="ambient s-idle" aria-hidden="true"></div>
<div class="scrim top" aria-hidden="true"></div>
<nav class="toolbar" aria-label="{plain.NAME}">
  <div class="tgroup glass"><a class="brand" href="/">{icons.mark()}<span>{plain.NAME}</span></a></div>
  <div class="tgroup glass"><a class="tbtn" href="/" aria-label="New run">{icons.plus(16)}<span class="lbl">New run</span></a>
    <a class="tbtn" href="/checks" aria-label="System check">{icons.check(16)}<span class="lbl">System check</span></a></div>
</nav>
<main>
<header class="hero"><h1>Connect</h1>
  <p class="lede">Connect a GitHub repository: it works out how the repo installs and runs its tests, builds a test sandbox for it, and checks the tests pass there. After that, you can run any issue from that repo. Below, the advisors who review each run.</p></header>
<section class="group" aria-labelledby="h-repo"><h2 id="h-repo">GitHub repository</h2>
  <div class="sect"><div class="field"><input id="link" type="url" inputmode="url" placeholder="https://github.com/owner/repo" autocomplete="off" aria-label="GitHub repository link" value="{e(f'https://github.com/{repo}' if repo else '')}">
    <button type="button" class="btn glass prominent" id="go">Connect</button></div></div>
  <p class="foot">Public repos only, in JavaScript, TypeScript or Python. Building the test sandbox uses a few cents of E2B credit.</p>
  <p class="err" id="err" role="alert"></p></section>
{_connect_progress(repo)}
<section class="group" aria-labelledby="h-conn"><h2 id="h-conn">Connected repos</h2>
  <div class="sect"><ul class="rows">{rows}</ul></div>
  {'<p class="foot">Connect again to move a repo to its latest code. Its current setup keeps working until the new one is ready.</p>' if 'class="btn glass again"' in rows else ''}</section>
{_advisor_cards()}
</main>
<script src="/static/topo.js" defer></script><script src="/static/glass.js" defer></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/gsap.min.js" defer></script>
<script src="/static/advisors.js" defer></script>
<script>
const H = {{ "{TOKEN_HEADER}": {json.dumps(TOKEN)}, "Content-Type": "application/json" }};
const $ = id => document.getElementById(id);
$("link").addEventListener("keydown", ev => {{ if (ev.key === "Enter") $("go").click(); }});
async function connect(url, again, btn) {{
  $("err").textContent = ""; btn.disabled = true;
  try {{
    const r = await fetch("/api/connect", {{ method: "POST", headers: H, body: JSON.stringify({{ url, again }}) }});
    const j = await r.json();
    if (!r.ok) {{ btn.disabled = false; $("err").textContent = j.error || "Something went wrong."; return; }}
    location.href = "/connect?repo=" + encodeURIComponent(j.repo);
  }} catch (ex) {{ btn.disabled = false; $("err").textContent = "Could not reach {plain.NAME}. Is it still running?"; }}
}}
$("go").onclick = () => connect($("link").value.trim(), false, $("go"));
const pick = seat => {{
  document.querySelectorAll(".adv-tile").forEach(t => {{ const on = t.dataset.seat === seat; t.classList.toggle("on", on); t.setAttribute("aria-selected", String(on)); }});
  document.querySelectorAll(".adv-track .mark").forEach(m => m.classList.toggle("on", m.dataset.seat === seat));
  document.querySelectorAll(".adv-panel").forEach(p => {{ p.hidden = p.dataset.seat !== seat; }});
  const panel = document.getElementById("adv-panel-" + seat);
  if (panel && window.gsap && !matchMedia("(prefers-reduced-motion: reduce)").matches)
    gsap.fromTo(panel, {{ opacity: 0, y: 10 }}, {{ opacity: 1, y: 0, duration: .45, ease: "power3.out", clearProps: "transform,opacity" }});
  try {{ localStorage.setItem("da-advisor", seat); }} catch (e) {{}}
}};
document.addEventListener("click", ev => {{ const t = ev.target.closest(".adv-pick"); if (t) pick(t.dataset.seat); }});
try {{ const saved = localStorage.getItem("da-advisor"); if (saved && document.getElementById("adv-panel-" + saved)) pick(saved); }} catch (e) {{}}
const firstMark = document.querySelector(".adv-tile.on"); if (firstMark) document.querySelectorAll(".adv-track .mark").forEach(m => m.classList.toggle("on", m.dataset.seat === firstMark.dataset.seat));
document.addEventListener("click", async ev => {{
  const b = ev.target.closest(".adv-ask button"); if (!b) return;
  const box = b.closest(".adv-ask"), out = box.querySelector(".adv-answer");
  const show = (ok, title, text) => {{ out.hidden = false; out.className = "adv-answer " + (ok ? "ok" : "bad"); out.textContent = "";
    const t = document.createElement("b"); t.textContent = title; const p = document.createElement("p"); p.textContent = text; out.append(t, p); }};
  const say = state => document.dispatchEvent(new CustomEvent("advisor-state", {{ detail: {{ seat: box.dataset.seat, state }} }}));
  b.disabled = true; say("asking"); show(true, "Asking…", "Waiting for the advisors' server.");
  try {{
    const fields = {{}}; box.querySelectorAll("input[name], textarea[name]").forEach(x => {{ fields[x.name] = x.value; }});
    const r = await fetch("/api/advisors-ask", {{ method: "POST", headers: H, body: JSON.stringify({{ seat: box.dataset.seat, fields }}) }});
    const j = await r.json();
    if (r.ok) {{
      show(true, box.dataset.seat + " said", j.said); say("answered");
      if (j.full) {{ const d = document.createElement("details"); d.className = "fa-wrap";   // built and escaped by the server
        const sm = document.createElement("summary"); sm.textContent = "Full answer"; d.appendChild(sm);
        const body = document.createElement("div"); body.innerHTML = j.full; d.appendChild(body); out.appendChild(d); }}
    }}
    else {{ show(false, "Not asked", j.error || "Something went wrong."); say("failed"); }}
  }} catch (ex) {{ show(false, "Not asked", "Could not reach {plain.NAME}."); say("failed"); }}
  finally {{ b.disabled = false; }}
}});
document.querySelectorAll("button.again").forEach(b => b.onclick = () => connect("https://github.com/" + b.dataset.repo, true, b));
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


def _label_queue() -> str:
    """Issues labelled on GitHub (autostart.py): waiting, started, or not started and why. Hidden when off and empty."""
    from . import autostart, icons
    e, items = viewer.e, autostart.recent()
    if not items and not autostart.enabled():
        return ""
    rows = []
    for it in items:
        st = it.get("status")
        cls, word = {"queued": ("waiting", "Waiting to start"), "started": ("done", "Started"),
                     "refused": ("stopped", "Not started")}.get(st, ("pending", st))
        sub = word + (f": {it['why']}" if it.get("why") else "")
        inner = (f'<span class="ic">{icons.STATE.get(cls, icons.pause)()}</span><span class="t"><b>{e(it["repo"])} '
                 f'#{it["number"]} {e(it.get("title", ""))}</b><span>{e(sub)}</span></span>')
        rows.append(f'<li><a class="row {cls}" href="/run/{e(it["run_id"])}">{inner}<span class="tr">{icons.chevron()}</span></a></li>'
                    if it.get("run_id") else f'<li class="row {cls}">{inner}</li>')
    state = (f"On. Add the label <b>{e(autostart.LABEL)}</b> to an issue in a connected repo and it starts here, one at a time."
             if autostart.enabled() else "Off.")
    return (f'<section class="group" aria-labelledby="h-auto"><h2 id="h-auto">Started from GitHub</h2>'
            f'<p class="foot">{state}</p>'
            + (f'<div class="sect"><ul class="rows">{"".join(rows)}</ul></div>' if rows else "") + "</section>")


def home_page(user: str = "") -> str:
    from . import icons
    from .store import reachable
    e = viewer.e
    rows, db_up = [], reachable()
    for rid in _runs():
        if db_up:
            got = _status_of(rid)
        else:  # each run's state is in the database: list the runs without it rather than wait on it
            got = ("", "Status unavailable: the database is off") if (CFG.runs_dir / rid / "console.log").exists() else None
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
    <a class="tbtn" href="/connect" aria-label="Connect">{icons.link(16)}<span class="lbl">Connect</span></a>
    <a class="tbtn" href="/how" aria-label="How it works">{icons.play(16)}<span class="lbl">How it works</span></a>
    <a class="tbtn" href="/checks" aria-label="System check">{icons.check(16)}<span class="lbl">System check</span></a></div>
  {f'<div class="tgroup glass"><a class="tbtn" href="/logout" title="Signed in with GitHub as {e(user)}" aria-label="Sign out ({e(user)})"><span class="lbl">{e(user)}</span><span>Sign out</span></a></div>' if user else ''}
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
    <div class="sect auto-pick" id="auto" hidden><p><b>It will prove:</b> <span id="auto-text"></span></p>
      <p class="foot"><span id="auto-from"></span> · <button type="button" class="linkbtn" id="auto-change">Change</button></p></div>
    <div class="sect" id="sections" role="radiogroup" aria-labelledby="h-sec"></div></section>
  <section class="group" aria-labelledby="h-ai" style="margin-top:28px"><h2 id="h-ai">Which AI?</h2>
    <div class="seg glass" role="radiogroup" aria-labelledby="h-ai">
      <label><input type="radio" name="ai" value="standard" checked>Standard<small>A few cents</small></label>
      <label><input type="radio" name="ai" value="opus">Claude Opus<small>About $0.70</small></label>
    </div>
    <p class="foot" id="aifoot">Cheaper. It often cannot fix the bug.</p></section>
  <section class="group" aria-labelledby="h-ptr" style="margin-top:28px"><h2 id="h-ptr">Your pointers <span class="opt">Optional</span></h2>
    <div class="sect pointers">
      <label class="pfield"><b>Where to look for the cause</b>
        <input id="look-in" type="text" placeholder="packages/ai/src/ui/chat.ts" autocomplete="off" spellcheck="false" autocapitalize="off">
        <small>Files you suspect, separated by commas, from the repo's top folder (a GitHub file link works too). They are searched first and named to the AI that finds the cause; it still goes where the evidence points.</small></label>
      <label class="pfield"><b>Write the unit test in</b>
        <input id="test-in" type="text" placeholder="packages/ai/src/ui/chat.test.ts" autocomplete="off" spellcheck="false" autocapitalize="off">
        <small>An existing test file. The unit test is added to it as new cases at the end; nothing else in it changes. Leave empty for a new test file beside the code.</small></label>
    </div></section>
  <div class="start" style="margin-top:22px"><button type="button" class="btn glass prominent" id="start">{icons.play(16)}<span>Start the run</span></button></div>
</div>
{_label_queue()}
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
$("auto-change").onclick = () => {{ $("auto").hidden = true; $("sections").hidden = false; }};
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
    const pickH = (j.auto || {{}}).heading || "";
    add("", "The problem in the title", j.title, pickH === "");
    j.sections.forEach(x => add(x.heading, x.heading, x.preview, x.heading === pickH));
    const one = j.auto && j.auto.single;          // one problem: say what it will prove; the list stays one tap away
    $("auto").hidden = !one; box.hidden = !!one;
    if (one) {{ $("auto-text").textContent = j.auto.preview; $("auto-from").textContent = "Picked from " + j.auto.from; }}
    $("more").hidden = false;
  }} catch (ex) {{ fail("Could not reach {plain.NAME}. Is it still running?"); }}
  finally {{ $("check").disabled = false; }}
}};
$("start").onclick = async () => {{
  fail(""); $("start").disabled = true;
  const sec = document.querySelector('input[name="sec"]:checked'), ai = document.querySelector('input[name="ai"]:checked');
  try {{
    const r = await fetch("/api/start", {{ method: "POST", headers: {{ ...H, "Content-Type": "application/json" }},
      body: JSON.stringify({{ url: $("link").value.trim(), heading: sec ? sec.value : "", ai: ai ? ai.value : "",
                             look_in: $("look-in").value, test_in: $("test-in").value }}) }});
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
              cookie: list | None = None):
        b = body.encode()
        self.send_response(code)
        for c in cookie or []:
            self.send_header("Set-Cookie", c)
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

    def _cookie(self, name: str) -> str:
        from http.cookies import CookieError, SimpleCookie
        try:
            c = SimpleCookie(self.headers.get("Cookie") or "")
        except CookieError:
            return ""
        return c[name].value if name in c else ""

    def user(self) -> str:
        """The GitHub account signed in, or ""."""
        from . import ghauth
        return ghauth.session_user(self._cookie("da_session")) if ghauth.configured() else ""

    def _signed_in(self) -> bool:
        """GitHub sign-in not set up (this Mac): always. Set up: only with a valid session for an account on the list.
        (A public address without it never gets here: it stays locked.)"""
        from . import ghauth
        return not ghauth.configured() or bool(self.user())

    def _base(self) -> str:
        return f"https://{_public_host()}" if _public_host() else f"http://{(self.headers.get('Host') or '').lower()}"

    def _secure(self) -> str:
        return "; Secure" if (_public_host() or self.headers.get("X-Forwarded-Proto") == "https") else ""

    def _auth(self, parts: list, q: dict):
        """/login, /auth/github (off to GitHub), /auth/github/callback (back from it), /logout."""
        from . import ghauth
        if parts == ["login"]:
            return self._send(200, login_page((q.get("error") or [""])[0]))
        if parts == ["logout"]:
            return self._send(302, "", "text/plain", location="/login",
                              cookie=[f"da_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0{self._secure()}"])
        if not ghauth.configured():
            return self._send(302, "", "text/plain", location="/login?error=off")
        redirect_uri = self._base() + "/auth/github/callback"
        if parts == ["auth", "github"]:
            state, value = ghauth.new_state((q.get("next") or ["/"])[0])
            return self._send(302, "", "text/plain", location=ghauth.authorize_url(state, redirect_uri),
                              cookie=[f"da_oauth={value}; HttpOnly; SameSite=Lax; Path=/auth; Max-Age={ghauth.STATE_S}{self._secure()}"])
        clear = f"da_oauth=; HttpOnly; SameSite=Lax; Path=/auth; Max-Age=0{self._secure()}"
        if (q.get("error") or [""])[0] == "access_denied":
            return self._send(302, "", "text/plain", location="/login?error=cancelled", cookie=[clear])
        nxt = ghauth.check_state(self._cookie("da_oauth"), (q.get("state") or [""])[0])
        if nxt is None:
            return self._send(302, "", "text/plain", location="/login?error=state", cookie=[clear])
        try:
            login = ghauth.account_for((q.get("code") or [""])[0], redirect_uri).lower()   # names ignore case
        except ghauth.AuthError as ex:
            print(f"{plain.NAME}: GitHub sign-in failed: {ex}", flush=True)
            return self._send(302, "", "text/plain", location="/login?error=github", cookie=[clear])
        if login not in ghauth.allowed():
            print(f"{plain.NAME}: GitHub sign-in refused for an account not on the list", flush=True)
            return self._send(403, login_page(refused=login), cookie=[clear])
        return self._send(302, "", "text/plain", location=nxt, cookie=[
            clear, f"da_session={ghauth.make_session(login)}; HttpOnly; SameSite=Lax; Path=/; Max-Age={ghauth.SESSION_S}{self._secure()}"])

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
            if parts in (["login"], ["logout"], ["auth", "github"], ["auth", "github", "callback"]):
                return self._auth(parts, q)
            if len(parts) == 2 and parts[0] == "static" and parts[1] in STATIC_FILES:   # the app's own styles and scripts:
                return self._send(200, (viewer.STATIC / parts[1]).read_text(), STATIC_FILES[parts[1]])  # open (sign-in uses them)
            if not self._signed_in():
                from urllib.parse import quote
                nxt = u.path + (f"?{u.query}" if u.query else "")
                return self._send(302, "", "text/plain", location="/login" if nxt == "/" else f"/auth/github?next={quote(nxt)}")
            if not parts:
                return self._send(200, home_page(self.user()))
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
                from .store import reachable
                if not reachable():
                    return self._send(503, "The database is off, so this run can't be read. On this Mac it runs in "
                                           "Docker Desktop: open it, wait a minute, then reload.", "text/plain; charset=utf-8")
                if parts[0] == "run":
                    return self._send(200, viewer.render(viewer.gather(rid), mode="live", token=TOKEN))
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

    def _github_hook(self):
        """GitHub's webhook: no sign-in (GitHub can't), so only a delivery signed with the shared secret is read."""
        from . import autostart
        if not autostart.secret():
            return self._json(503, {"error": "the webhook is not set up (GITHUB_WEBHOOK_SECRET)"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            return self._json(413, {"error": "too large"})
        body = self.rfile.read(n)
        if not autostart.signature_ok(body, self.headers.get("X-Hub-Signature-256")):
            return self._json(401, {"error": "bad signature"})
        try:
            code, said = autostart.on_event(self.headers.get("X-GitHub-Event") or "", json.loads(body or b"{}"))
        except Exception as ex:
            return self._json(500, {"error": f"could not queue it ({type(ex).__name__})"})
        return self._json(code, {"result": said})

    def do_POST(self):  # start a run, connect a repo, your OK, ask an advisor, or a GitHub webhook
        self.server.last = time.monotonic()
        if getattr(self.server, "locked", ""):
            return self._send(503, f"{plain.NAME} is locked. {self.server.locked}", "text/plain; charset=utf-8")
        if urlparse(self.path).path == "/hooks/github":
            return self._github_hook()
        if (not self._host_ok() or not self._token_ok() or not self._origin_ok() or not self._signed_in()
                or not (self.headers.get("Content-Type") or "").startswith("application/json")):
            return self._json(403, {"error": "Not allowed. Start runs from the DebugAssistAgent home page."})
        path = urlparse(self.path).path
        if path not in ("/api/start", "/api/connect", "/api/advisors-ask", "/api/decide"):
            return self._json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4096:
            return self._json(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            if path == "/api/decide":
                said = decide(str(body.get("run_id", "")), str(body.get("decision", "")), str(body.get("sha256", "")),
                              str(body.get("commit_message", ""))[:12000], by=self.user())
                return self._json(200, {"said": said})
            if path == "/api/advisors-ask":
                from .advisors import AdvisorError, ask, status
                if status()[0] != "ON":
                    raise Refused("The advisors are not switched on.")
                try:
                    fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
                    said = ask(str(body.get("seat", "")), {str(k): str(v)[:20000] for k, v in fields.items()})
                except ValueError as ex:
                    raise Refused(str(ex).capitalize() + ".")
                except (AdvisorError, OSError) as ex:
                    return self._json(502, {"error": f"The advisor did not answer: {ex}"[:300]})
                raw = getattr(said, "raw", None)
                return self._json(200, {"said": str(said), **({"full": viewer.full_answer(raw)} if raw else {})})
            if path == "/api/connect":
                repo = start_connect(str(body.get("url", "")), again=bool(body.get("again")))
                return self._json(200, {"repo": repo, "page": f"/connect?repo={repo}"})
            rid = start_run(str(body.get("url", "")), str(body.get("heading", "")), str(body.get("ai", "")),
                            str(body.get("look_in", "")), str(body.get("test_in", "")))
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
    from . import ghauth
    if host in ("127.0.0.1", "localhost"):
        return ""
    if not ghauth.configured():
        return (f"GitHub sign-in is not set up ({', '.join(ghauth.missing())} missing). Set it in Render ▸ Environment, "
                "then redeploy.")
    if not _public_host():
        return "The public address is unknown. Set PUBLIC_HOST in Render ▸ Environment, then redeploy."
    return ""


def _gh_on() -> bool:
    from . import ghauth
    return ghauth.configured()


def serve(port: int = PORT, idle_s: int = IDLE_S, host: str = "127.0.0.1") -> None:
    why = locked_reason(host)
    print(f"{plain.NAME}: starting on {host}:{port}; public address {_public_host() or '(none)'}; "
          + (f"LOCKED: {why}" if why else ("GitHub sign-in required" if _gh_on() else "no sign-in (this Mac only)")), flush=True)
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
    from . import autostart
    if autostart.enabled() and not why:
        ai = os.environ.get("AUTO_RUN_AI", "standard")
        threading.Thread(target=autostart.worker, args=(lambda link: start_run(link, "", ai), run_going,
                                                         threading.Event()), daemon=True).start()
        print(f"{plain.NAME}: automatic runs ON: issues labelled '{autostart.LABEL}' in connected repos start a run "
              f"({ai} AI, at most {autostart.MAX_PER_DAY} a day)", flush=True)
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
