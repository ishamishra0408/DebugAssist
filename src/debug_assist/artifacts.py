"""A run's files in MongoDB, for hosts whose disk is wiped on restart (Render's free plan). On by RUN_FILES_IN_DB=1.

After every step, everything in runs/<id>/ is saved to the `run_files` collection, except the code copies, which are
saved as only what the run changed against the pinned commit (a few files, not the repository). Before a step, an
approval or the page reads them, anything missing on disk is put back: files as they were, code copies rebuilt from
the base checkout plus the run's changes. Paths stay the same, so the run's state needs no change.
"""
import hashlib
from pathlib import Path

from bson.binary import Binary

from .config import CFG

MAX_FILE = 4 * 1024 * 1024   # draft tests, patches and logs are kilobytes; anything this big is not run evidence
SKIP_DIRS = {"node_modules", ".pnpm-store", ".corepack", ".bin", ".git", "dist", ".da-logs", "sandbox"}


def run_ids() -> list[str]:
    """Runs whose files are saved here (the run list on a host whose disk was wiped)."""
    return list(_coll().distinct("run_id")) if enabled() else []


def enabled() -> bool:
    import os
    return os.environ.get("RUN_FILES_IN_DB", "").strip() in ("1", "yes", "true")


def _coll():
    from .store import db
    return db()["run_files"]


def _copies(run_dir: Path) -> list[Path]:
    """Code copies inside a run folder: git checkouts (checkout, holdout-base, ...)."""
    return [d for d in run_dir.iterdir() if d.is_dir() and (d / ".git").exists()] if run_dir.exists() else []


def save_run(run_id: str) -> int:
    """Save the run folder: plain files as they are, code copies as their changed files. Unchanged files are skipped."""
    run_dir = CFG.runs_dir / run_id
    if not run_dir.exists():
        return 0
    coll, saved, seen = _coll(), 0, set()
    copies = _copies(run_dir)

    def put(rel: str, data: bytes | None, kind: str):
        nonlocal saved
        _id = f"{run_id}/{rel}"
        seen.add(_id)
        sha = hashlib.sha1(data).hexdigest() if data is not None else "deleted"
        old = coll.find_one({"_id": _id}, {"sha": 1})
        if old and old.get("sha") == sha:
            return
        coll.replace_one({"_id": _id}, {"_id": _id, "run_id": run_id, "path": rel, "kind": kind, "sha": sha,
                                        "data": Binary(data) if data is not None else None}, upsert=True)
        saved += 1

    for f in run_dir.rglob("*"):
        if not f.is_file() or any(c in f.parents for c in copies) or set(f.relative_to(run_dir).parts) & SKIP_DIRS:
            continue
        if f.stat().st_size <= MAX_FILE:
            put(str(f.relative_to(run_dir)), f.read_bytes(), "file")
    from .sandbox_e2b import changed_files
    for c in copies:
        for p in changed_files(c):
            f = c / p
            put(f"{c.name}/{p}", f.read_bytes() if f.is_file() else None, "change")
        put(f"{c.name}/.copy", b"", "copy")  # the copy exists, even when nothing in it changed yet
    # a change the run undid (a reverted fix, a shelved draft) no longer differs: forget it
    for doc in coll.find({"run_id": run_id, "kind": "change"}, {"_id": 1}):
        if doc["_id"] not in seen:
            coll.delete_one({"_id": doc["_id"]})
    return saved


def restore_run(run_id: str) -> int:
    """Put back whatever of the run folder is missing on disk. Returns how many files were written."""
    from .checkout import run_copy
    from .profiles import PROFILES
    coll, run_dir, wrote = _coll(), CFG.runs_dir / run_id, 0
    docs = list(coll.find({"run_id": run_id}))
    if not docs:
        return 0
    copies = {d["path"].split("/", 1)[0] for d in docs if d["kind"] == "copy"}
    prof = None
    for name in copies:
        dest = run_dir / name
        if not dest.exists():
            prof = prof or _profile_of(run_id) or next(iter(PROFILES.values()))
            run_copy(prof, dest, _code_of(run_id))  # the run's commit (main when it started), unmodified; its changes follow
            for d in docs:
                if d["kind"] == "change" and d["path"].startswith(name + "/"):
                    f = run_dir / d["path"]
                    if d["data"] is None:
                        f.unlink(missing_ok=True)
                    else:
                        f.parent.mkdir(parents=True, exist_ok=True)
                        f.write_bytes(bytes(d["data"]))
                    wrote += 1
    for d in docs:
        if d["kind"] == "file":
            f = run_dir / d["path"]
            if not f.exists():
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(bytes(d["data"]))
                wrote += 1
    return wrote


def _code_of(run_id: str) -> dict | None:
    try:
        from .viewer import _app
        return (_app().get_state({"configurable": {"thread_id": run_id}}).values or {}).get("code")
    except Exception:
        return None


def _profile_of(run_id: str):
    from .profiles import PROFILES
    try:
        from .viewer import _app
        repo = ((_app().get_state({"configurable": {"thread_id": run_id}}).values or {}).get("profile") or {}).get("repo")
        from . import profiles
        return profiles.get(repo) if repo else None
    except Exception:
        return None
