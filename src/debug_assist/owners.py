"""Whose run it is, and what each person has spent (Isha 2026-10-09: "my runs should not be visible to him, and his
not to me"; and a spending limit per person).

  run_owners (MongoDB, so it survives a wiped host disk)   {_id: run id, owner: GitHub name, started_at}
  OLDER_RUNS_OWNER     runs from before owners were recorded belong to this GitHub name (default: the first name in
                       ALLOWED_GITHUB_USERS); the record is written the first time that person lists them
  SPEND_CAP_USD        each person's AI spend per calendar month (UTC), default $10
  SPEND_CAPS           a different limit for named people: "devpath56=5, ishamishra0408=40"

The spend is what the run meters recorded (the AI calls), not E2B compute. Connected repos and the system check stay
shared: a repo is set up once for everyone.
"""
import os
from datetime import datetime, timezone

DEFAULT_CAP_USD = 10.0


def _coll():
    from .store import db
    return db()["run_owners"]


def older_owner() -> str:
    from .ghauth import allowed
    name = os.environ.get("OLDER_RUNS_OWNER", "").strip().lstrip("@").lower()
    if name:
        return name
    first = [x.strip().lstrip("@").lower() for x in os.environ.get("ALLOWED_GITHUB_USERS", "").split(",") if x.strip()]
    return first[0] if first and first[0] in allowed() else ""


def record(run_id: str, owner: str) -> None:
    if owner:
        _coll().update_one({"_id": run_id}, {"$setOnInsert": {"owner": owner.lower(),
                                                              "started_at": datetime.now(timezone.utc).isoformat()}},
                           upsert=True)


def owners_of(run_ids: list[str]) -> dict:
    """{run id: owner} for every id; one without a record belongs to the older-runs owner."""
    got = {d["_id"]: d["owner"] for d in _coll().find({"_id": {"$in": list(run_ids)}}, {"owner": 1})}
    old = older_owner()
    return {r: got.get(r, old) for r in run_ids}


def owner_of(run_id: str) -> str:
    return owners_of([run_id])[run_id]


def claim_older(run_ids: list[str], who: str) -> None:
    """Runs from before owners were recorded, written down as the older-runs owner's the first time they list them
    (so reordering ALLOWED_GITHUB_USERS later can't hand them to someone else)."""
    if not who or who != older_owner():
        return
    have = {d["_id"] for d in _coll().find({"_id": {"$in": list(run_ids)}}, {"_id": 1})}
    for r in run_ids:
        if r not in have:
            record(r, who)


def cap_for(who: str) -> float:
    for part in os.environ.get("SPEND_CAPS", "").split(","):
        name, _, usd = part.partition("=")
        if name.strip().lstrip("@").lower() == who.lower() and usd.strip():
            try:
                return float(usd)
            except ValueError:
                break
    try:
        return float(os.environ.get("SPEND_CAP_USD", "") or DEFAULT_CAP_USD)
    except ValueError:
        return DEFAULT_CAP_USD


def spent_this_month(who: str, run_ids: list[str], now: datetime | None = None) -> float:
    """What this person's runs opened this calendar month (UTC) have spent, calls in flight included."""
    from . import meter
    from .meter import MICRO
    start = (now or datetime.now(timezone.utc)).strftime("%Y-%m-01")
    mine = [r for r, o in owners_of(run_ids).items() if o == who]
    total = 0
    for m in meter._db()["meters"].find({"_id": {"$in": mine}, "opened_at": {"$gte": start}},
                                       {"spent_micro": 1, "reserved_micro": 1}):
        total += (m.get("spent_micro") or 0) + (m.get("reserved_micro") or 0)
    return total / MICRO
