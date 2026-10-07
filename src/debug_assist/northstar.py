"""The north stars computed from the runs themselves, never typed into a document (de-advisor review 2026-10-07:
the spec carried ⏱ as n = 1 while the data held two values).

  ⏱ time to validated fix   over pipeline RUNS (meters with kind "run"; trials never count): which were picked up,
                             which reached a fix validated by two judges, and the seconds for each
  🎯 would-have-caught       north-star-v1: k of m earlier same-condition bugs. m comes from the condition corpus:
                             siblings found for the run's condition. m = 0 reads NOT SCORED
"""


def _graph_state(app, run_id: str) -> dict:
    return app.get_state({"configurable": {"thread_id": run_id}}).values or {}


def runs(db=None) -> list[dict]:
    from .graph import build
    from .store import db as _db
    d = db if db is not None else _db()
    app, out = build(), []
    for m in d["meters"].find({"kind": "run"}).sort("opened_at", 1):
        s = _graph_state(app, m["_id"])
        if not s:
            continue
        clock, fix = s.get("fix_clock") or {}, s.get("fix") or {}
        judges = clock.get("judges")
        if judges is None and fix.get("status") == "VALIDATED":  # runs from before the judges field
            judges = 2 if str((fix.get("holdout") or {}).get("status", "")).startswith("PASSED") else 1
        out.append({"run_id": m["_id"], "model": "opus" if s.get("demo") else "dev",
                    "picked_up": bool(clock.get("started_at")), "seconds": clock.get("seconds"),
                    "two_judges": fix.get("status") == "VALIDATED" and judges == 2,
                    "outcome": (s.get("outcome") or {}).get("exit"),
                    "siblings": len((s.get("backtest") or {}).get("candidates") or [])})
    return out


def time_to_validated_fix(rows: list[dict]) -> dict:
    picked = [r for r in rows if r["picked_up"]]
    done = [r for r in picked if r["two_judges"] and r["seconds"] is not None]
    secs = sorted(r["seconds"] for r in done)
    return {"pickups": len(picked), "reached": len(done), "seconds": secs,
            "range": (secs[0], secs[-1]) if secs else None, "runs": [r["run_id"] for r in done]}


def would_have_caught(rows: list[dict]) -> dict:
    m = sum(r["siblings"] for r in rows)
    return {"m": m, "state": "NOT SCORED: no past sibling (m = 0)" if m == 0 else f"{m} sibling(s) to score"}
