"""Automatic start: an issue labelled `debug-assist` in a connected repo is queued and run, one at a time.

  GitHub webhook   POST /hooks/github, signed with GITHUB_WEBHOOK_SECRET (HMAC-SHA256, X-Hub-Signature-256). An
                   "issues" event with action "labeled" and the label queues the issue. Unsigned or wrongly signed
                   requests are refused before the body is read as JSON.
  catch-up scan    on start and every SCAN_S while the server is up: open issues carrying the label in every connected
                   repo. The free host sleeps; the webhook wakes it but GitHub gives up after 10 s, so the scan is what
                   makes sure a labelled issue is not lost.
  queue            MongoDB `queue`, one document per issue ("owner/repo#123"). One automatic run per issue, ever: to
                   run it again, start it from the home page. At most MAX_PER_DAY automatic runs a day.
  worker           a thread in the server: when no run is going, it starts the oldest queued issue the same way the
                   home page does (standard AI, the issue's title as the problem to fix).

Off unless AUTO_RUNS=1, because every run spends money. Adding a label needs triage rights on the repo, so only the
repo's own people can start one. The pipeline still never writes to GitHub.
"""
import hashlib
import hmac
import os
import threading
import time
from datetime import datetime, timedelta, timezone

LABEL = os.environ.get("AUTO_LABEL", "debug-assist")
MAX_PER_DAY = int(os.environ.get("AUTO_MAX_PER_DAY", "10"))
SCAN_S = 600
TICK_S = 20


def enabled() -> bool:
    return os.environ.get("AUTO_RUNS", "").strip().lower() in ("1", "yes", "true")


def secret() -> str:
    return os.environ.get("GITHUB_WEBHOOK_SECRET", "")


def signature_ok(body: bytes, header: str | None, key: str | None = None) -> bool:
    key = secret() if key is None else key
    if not key or not header or not header.startswith("sha256="):
        return False
    want = "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coll():
    from .store import db
    return db()["queue"]


def enqueue(repo: str, number: int, title: str, source: str, coll=None) -> str:
    """'queued', or why not ('already queued or run', 'not connected')."""
    from .profiles import ready
    coll = _coll() if coll is None else coll
    if repo not in ready():
        return "not connected"
    _id = f"{repo}#{number}"
    if coll.find_one({"_id": _id}):
        return "already queued or run"
    coll.insert_one({"_id": _id, "repo": repo, "number": int(number), "title": (title or "")[:200], "source": source,
                     "status": "queued", "queued_at": _now().isoformat(), "run_id": "", "why": ""})
    return "queued"


def on_event(event: str, payload: dict, coll=None) -> tuple[int, str]:
    """(HTTP status, plain answer) for one webhook delivery, after its signature was checked."""
    if event == "ping":
        return 200, "pong"
    if event != "issues" or payload.get("action") != "labeled":
        return 202, "ignored: only an issue being labelled starts anything"
    if ((payload.get("label") or {}).get("name") or "") != LABEL:
        return 202, f"ignored: the label is not {LABEL}"
    issue, repo = payload.get("issue") or {}, (payload.get("repository") or {}).get("full_name", "")
    if "pull_request" in issue:
        return 202, "ignored: a pull request, not an issue"
    if not enabled():
        return 202, "ignored: automatic runs are off (AUTO_RUNS)"
    return 202, enqueue(repo, int(issue.get("number") or 0), issue.get("title", ""), "label", coll)


def scan(coll=None) -> int:
    """Queue every open issue that carries the label in a connected repo. Returns how many were added."""
    from . import github_read
    from .profiles import ready
    added = 0
    for repo in ready():
        for it in github_read.api(f"repos/{repo}/issues?labels={LABEL}&state=open&per_page=20") or []:
            if "pull_request" not in it and enqueue(repo, it["number"], it.get("title", ""), "scan", coll) == "queued":
                added += 1
    return added


def started_today(coll) -> int:
    since = (_now() - timedelta(days=1)).isoformat()
    return coll.count_documents({"status": "started", "started_at": {"$gte": since}})


def tick(start, busy, coll=None) -> dict | None:
    """Start the oldest queued issue if nothing is running. start(link) → run id; busy() → a run is going."""
    coll = _coll() if coll is None else coll
    if busy():
        return None
    item = next(iter(coll.find({"status": "queued"}).sort("queued_at", 1).limit(1)), None)
    if item is None:
        return None
    if started_today(coll) >= MAX_PER_DAY:
        coll.update_one({"_id": item["_id"]}, {"$set": {"why": f"waiting: {MAX_PER_DAY} automatic runs already today"}})
        return None
    link = f"https://github.com/{item['repo']}/issues/{item['number']}"
    try:
        rid = start(link)
        upd = {"status": "started", "started_at": _now().isoformat(), "run_id": rid, "why": ""}
    except Exception as ex:  # Refused (no longer connected, ...) or a crash: say why, never retry in a loop
        upd = {"status": "refused", "started_at": _now().isoformat(), "why": str(ex)[:300]}
    coll.update_one({"_id": item["_id"]}, {"$set": upd})
    return {**item, **upd}


def worker(start, busy, stop: threading.Event) -> None:
    last_scan = 0.0
    while not stop.is_set():
        try:
            if time.monotonic() - last_scan > SCAN_S:
                scan()
                last_scan = time.monotonic()
            tick(start, busy)
        except Exception as ex:  # MongoDB or GitHub down for a moment: try again next tick
            print(f"autostart: {type(ex).__name__}: {str(ex)[:200]}", flush=True)
        stop.wait(TICK_S)


def recent(limit: int = 10) -> list[dict]:
    try:
        from .config import CFG
        from .store import client
        return list(client(1500)[CFG.db_name]["queue"].find().sort("queued_at", -1).limit(limit))
    except Exception:
        return []
