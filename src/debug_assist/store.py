"""One MongoDB handle for the whole package (the graph, the spend meter, the event log).

Collections:
  ledger      approvals and condition freezes (append-only by convention)
  conditions  named conditions + embeddings, for finding past bugs of the same kind
  meters      one row per run: dollars and sandbox seconds, reserved BEFORE use (meter.py)
  calls       one row per model call: what it was allowed to cost and what it did cost (meter.py)
  events      one row per model call, sandbox command and Laya decision (events.py)
"""
import time
from functools import lru_cache

from pymongo import MongoClient

from .config import CFG

_seen = {"at": -1e9, "up": False}


@lru_cache(maxsize=4)  # one client per timeout; maxsize 1 made two callers replace each other's client on every call
def client(timeout_ms: int = 5000) -> MongoClient:
    return MongoClient(CFG.mongodb_uri, serverSelectionTimeoutMS=timeout_ms)


def reachable(ttl_s: float = 15.0) -> bool:
    """Is the database answering? Asked at most once every ttl_s seconds (one ping, 2 s at most), so a page never
    waits on a database that is off once per item it shows (2026-10-08: the home page took over a minute on the
    Mac with Docker Desktop closed, one timeout per run)."""
    now = time.monotonic()
    if now - _seen["at"] >= ttl_s:
        try:
            client(2000).admin.command("ping")
            _seen["up"] = True
        except Exception:
            _seen["up"] = False
        _seen["at"] = time.monotonic()
    return _seen["up"]


def db(name: str | None = None):
    return client()[name or CFG.db_name]
