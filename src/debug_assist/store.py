"""One MongoDB handle for the whole package (the graph, the spend meter, the event log).

Collections:
  ledger      approvals and condition freezes (append-only by convention)
  conditions  named conditions + embeddings, for finding past bugs of the same kind
  meters      one row per run: dollars and sandbox seconds, reserved BEFORE use (meter.py)
  calls       one row per model call: what it was allowed to cost and what it did cost (meter.py)
  events      one row per model call, sandbox command and Laya decision (events.py)
"""
from functools import lru_cache

from pymongo import MongoClient

from .config import CFG


@lru_cache(maxsize=1)
def client(timeout_ms: int = 5000) -> MongoClient:
    return MongoClient(CFG.mongodb_uri, serverSelectionTimeoutMS=timeout_ms)


def db(name: str | None = None):
    return client()[name or CFG.db_name]
