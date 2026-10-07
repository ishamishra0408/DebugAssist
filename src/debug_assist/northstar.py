"""The north stars computed from the runs themselves, never typed into a document (de-advisor review 2026-10-07:
the spec carried ⏱ as n = 1 while the data held two values).

  Definitions: north-star-v1.2, sealed 2026-10-07 (config.NORTH_STAR_SEAL).
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
        if judges is None:  # runs from before the judges field: the same rule, read from the stored second test
            from .fixer import judge_count
            judges = judge_count(fix.get("status", ""), fix.get("holdout"))
        out.append({"run_id": m["_id"], "model": "opus" if s.get("demo") else "dev",
                    "skeleton": fix.get("status") == "PLACEHOLDER",  # run before the fixer existed: could not reach
                    "picked_up": bool(clock.get("started_at")), "seconds": clock.get("seconds"),
                    "two_judges": judges == 2,
                    "outcome": (s.get("outcome") or {}).get("exit"),
                    "siblings": len((s.get("backtest") or {}).get("candidates") or [])})
    return out


def time_to_validated_fix(rows: list[dict]) -> dict:
    picked = [r for r in rows if r["picked_up"]]
    done = [r for r in picked if r["two_judges"] and r["seconds"] is not None]
    secs = sorted(r["seconds"] for r in done)
    by_model = {k: {"reached": sum(1 for r in done if r.get("model") == k),
                    "pickups": sum(1 for r in picked if r.get("model") == k and not r.get("skeleton"))}
                for k in sorted({r.get("model") for r in picked if r.get("model")})}
    return {"pickups": len(picked), "reached": len(done), "seconds": secs,
            "range": (secs[0], secs[-1]) if secs else None, "runs": [r["run_id"] for r in done],
            "skeleton": sum(1 for r in picked if r.get("skeleton")), "by_model": by_model}


def corpus_issues(db=None) -> list[str]:
    """The distinct issues the condition corpus holds: the population any sibling search can draw from."""
    from .store import db as _db
    d = db if db is not None else _db()
    return sorted(d["conditions"].distinct("issue_url"))


def would_have_caught(rows: list[dict], corpus: list[str] | None = None) -> dict:
    """m = candidate siblings found. They are unconfirmed, so k is never computed here: a non-author confirms them
    first (north-star-v1.1). With m = 0, say whether the search could have found one at all (independent grade
    2026-10-07: the corpus held only the incident's own issue, so "no past sibling" alone overstated it)."""
    m = sum(r["siblings"] for r in rows)
    if m:
        return {"m": m, "state": f"NOT SCORED YET: {m} candidate sibling(s), unconfirmed (k needs a non-author's check)"}
    state = "NOT SCORED: no past sibling (m = 0)"
    if corpus is not None and len(corpus) <= 1:
        state += (f"; the condition corpus holds {len(corpus)} issue{'' if len(corpus) == 1 else 's'}"
                  f"{' (this one)' if len(corpus) == 1 else ''}, so no search could have found a sibling")
    return {"m": m, "corpus": len(corpus) if corpus is not None else None, "state": state}
