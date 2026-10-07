"""One event log: a MongoDB row for every model call, sandbox command and Laya decision, keyed by run_id.

Phoenix shows the same run as traces (the CLI tags every span with the run_id as its session). This log is the
plain record you can query without Phoenix running: `db.events.find({run_id: ...}).sort({at: 1})`.

Each pipeline step binds (run_id, step) for its duration. models.py and sandbox.py read that binding, so every call
made inside a step is logged and metered without passing ids around. Outside a step (tests, preflight) nothing is
bound, nothing is logged.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

CURRENT: ContextVar[dict | None] = ContextVar("debug_assist_run", default=None)
_db_name: str | None = None  # tests point this at a scratch database


def current() -> dict | None:
    return CURRENT.get()


@contextmanager
def bind(run_id: str, step: str):
    token = CURRENT.set({"run_id": run_id, "step": step})
    try:
        yield
    finally:
        CURRENT.reset(token)


def log(kind: str, **fields) -> dict | None:
    """kind: model_call | sandbox | laya. Writes nothing when no run is bound."""
    ctx = CURRENT.get()
    if not ctx:
        return None
    from .store import db
    row = {"run_id": ctx["run_id"], "step": ctx["step"], "kind": kind,
           "at": datetime.now(timezone.utc).isoformat(), **fields}
    db(_db_name)["events"].insert_one(dict(row))
    return row


def for_run(run_id: str) -> list[dict]:
    from .store import db
    return list(db(_db_name)["events"].find({"run_id": run_id}, {"_id": 0}).sort("at", 1))
