"""Spend stop that survives a crash: dollars and sandbox seconds are RESERVED in MongoDB before use, settled after.

Why not keep the spend in the run state (as the skeleton did): LangGraph saves state only when a step finishes, so
a crash halfway through a step forgot every call that step had made, and the resumed run started under-counted.

Here each run has one `meters` row. A reservation is ONE atomic update that only succeeds if
spent + already reserved + this call's worst case stays within the cap. So:
  - the check happens BEFORE the call: no call starts that could cross the cap;
  - a crash between reserve and settle leaves the reservation counted (fails closed, never open);
  - a resumed run reads its spend from MongoDB, not from the checkpoint.
Money is kept in integer micro-dollars (exact sums, no float drift), always rounded up.

`calls` holds one row per model call: reserved → settled (or failed). A row stuck at "reserved" is a crash's trace.
"""
import math
import uuid
from datetime import datetime, timezone

from pymongo import ReturnDocument

from .budget import BudgetExceeded, TurnCapExceeded
from .config import TURN_CAPS

MICRO = 1_000_000
_db_name: str | None = None  # tests point this at a scratch database


class SandboxTimeExceeded(RuntimeError):
    pass


def _db():
    from .store import db
    return db(_db_name)


def micro(usd: float) -> int:
    return math.ceil(round(usd * MICRO, 6))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_run(run_id: str, cap_usd: float, sandbox_cap_s: int) -> dict:
    """Create the run's meter once. Calling it again (resume) changes nothing: caps are fixed at the first open."""
    _db()["meters"].update_one({"_id": run_id}, {"$setOnInsert": {
        "cap_micro": micro(cap_usd), "spent_micro": 0, "reserved_micro": 0,
        "sandbox_cap_s": int(sandbox_cap_s), "sandbox_used_s": 0, "sandbox_reserved_s": 0, "opened_at": _now()}},
        upsert=True)
    return snapshot(run_id)


def snapshot(run_id: str) -> dict | None:
    d = _db()["meters"].find_one({"_id": run_id})
    if d:
        d["spent_usd"] = d["spent_micro"] / MICRO
        d["reserved_usd"] = d["reserved_micro"] / MICRO
        d["cap_usd"] = d["cap_micro"] / MICRO
    return d


# ── turns ────────────────────────────────────────────────────────────────────────────────────────
def take_turn(run_id: str, step: str) -> dict:
    """Count one model call for a step, atomically, refusing past the step's cap. Kept in MongoDB with the money,
    so a step that crashes and is re-run on resume does not get its turns back. Returns all turns so far."""
    cap = TURN_CAPS[step]
    d = _db()["meters"].find_one_and_update(
        {"_id": run_id, "$or": [{f"turns.{step}": {"$lt": cap}}, {f"turns.{step}": {"$exists": False}}]},
        {"$inc": {f"turns.{step}": 1}}, return_document=ReturnDocument.AFTER)
    if d is None:
        used = ((snapshot(run_id) or {}).get("turns") or {}).get(step, 0)
        raise TurnCapExceeded(f"{step}: {used} calls already, cap {cap}; refused before calling")
    return d["turns"]


# ── dollars ──────────────────────────────────────────────────────────────────────────────────────
def reserve_call(run_id: str, step: str, model: str, worst_usd: float, max_tokens: int) -> dict:
    amount = micro(worst_usd)
    d = _db()["meters"].find_one_and_update(
        {"_id": run_id, "$expr": {"$lte": [{"$add": ["$spent_micro", "$reserved_micro", amount]}, "$cap_micro"]}},
        {"$inc": {"reserved_micro": amount}}, return_document=ReturnDocument.AFTER)
    if d is None:
        s = snapshot(run_id)
        if s is None:
            raise BudgetExceeded(f"run {run_id} has no meter; refusing an unmetered call")
        raise BudgetExceeded(f"{step}: this call could cost up to ${amount / MICRO:.4f}; spent ${s['spent_usd']:.4f}"
                             f" + reserved ${s['reserved_usd']:.4f} + that would pass the ${s['cap_usd']:.2f} cap."
                             " Refused BEFORE calling")
    call = {"_id": uuid.uuid4().hex, "run_id": run_id, "step": step, "model": model, "max_tokens": max_tokens,
            "reserved_micro": amount, "status": "reserved", "at": _now()}
    _db()["calls"].insert_one(dict(call))
    return call


def settle_call(call: dict, usage: dict | None, actual_usd: float | None, ms: int, error: str | None = None) -> dict:
    """actual_usd None (the call errored, cost unknown) → charge the full reservation: fail closed."""
    actual = call["reserved_micro"] if actual_usd is None else micro(actual_usd)
    d = _db()["meters"].find_one_and_update(
        {"_id": call["run_id"]}, {"$inc": {"reserved_micro": -call["reserved_micro"], "spent_micro": actual}},
        return_document=ReturnDocument.AFTER)
    _db()["calls"].update_one({"_id": call["_id"]}, {"$set": {
        "status": "failed" if error else "settled", "actual_micro": actual, "ms": ms, "error": error,
        "input_tokens": (usage or {}).get("input_tokens"), "output_tokens": (usage or {}).get("output_tokens"),
        "overran_reservation": actual > call["reserved_micro"], "settled_at": _now()}})
    return {"spent_usd": d["spent_micro"] / MICRO, "actual_usd": actual / MICRO}


# ── sandbox seconds ──────────────────────────────────────────────────────────────────────────────
def reserve_seconds(run_id: str, wanted: int, floor: int = 30) -> int:
    """Grant up to `wanted` seconds of sandbox time from what the run has left. The last command gets whatever
    remains (if at least `floor`); below that, refuse."""
    m = _db()["meters"]
    for _ in range(5):  # optimistic retry if two commands reserve at once
        d = m.find_one({"_id": run_id})
        if d is None:
            raise SandboxTimeExceeded(f"run {run_id} has no meter; refusing an unmetered sandbox command")
        left = d["sandbox_cap_s"] - d["sandbox_used_s"] - d["sandbox_reserved_s"]
        grant = min(int(wanted), left)
        if grant < floor:
            raise SandboxTimeExceeded(f"sandbox time cap reached: {d['sandbox_used_s']} s used of "
                                      f"{d['sandbox_cap_s']} s ({left} s left, a command needs at least {floor} s)")
        ok = m.find_one_and_update(
            {"_id": run_id, "$expr": {"$lte": [{"$add": ["$sandbox_used_s", "$sandbox_reserved_s", grant]},
                                               "$sandbox_cap_s"]}},
            {"$inc": {"sandbox_reserved_s": grant}})
        if ok:
            return grant
    raise SandboxTimeExceeded("could not reserve sandbox time (contention)")


def settle_seconds(run_id: str, reserved: int, used_s: float) -> None:
    _db()["meters"].update_one({"_id": run_id}, {"$inc": {"sandbox_reserved_s": -reserved,
                                                          "sandbox_used_s": math.ceil(used_s)}})
