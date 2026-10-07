"""Command line.

  uv run debug-assist preflight <issue-url> [--demo] [--no-trace]   check every dependency, start nothing
  uv run debug-assist run <issue-url> [--demo] [--no-trace]         preflight, then run until it pauses or stops
  uv run debug-assist resume <run-id> [--no-trace]                  preflight, then continue after a crash
  uv run debug-assist approve <run-id>                              your go-word (approves the exact PR text shown)
  uv run debug-assist reject <run-id>                               stop without publishing
  uv run debug-assist status <run-id>                               show the run
  uv run debug-assist events <run-id>                               every model call, sandbox command and decision
  uv run debug-assist cleanup [--yes]                               delete finished runs' code copies (dry run without --yes)
  uv run debug-assist view <run-id> [--watch]                       the run viewer: runs/<run-id>/view.html (read-only)
  uv run debug-assist serve [--port=8777]                           the run viewer on localhost, live (run/resume open it)

--focus=TEXT            the one problem in the issue to reproduce (default: the issue's title)
--focus-heading=HEADING the same, taken from the issue's section under that markdown heading
--demo      use the demo model (Claude Opus) under the demo cap ($2.50) instead of Qwen3-Coder-Next ($0.50 cap)
--no-trace  allowed only on purpose: the run proceeds without Phoenix and records trace OFF
--no-view   run/resume: don't start the localhost viewer or open the run's page
"""
import json
import sys
from contextlib import ExitStack
from datetime import datetime

COMMANDS = {"preflight", "run", "resume", "approve", "reject", "status", "events", "cleanup", "view", "serve"}


def _tracing():
    from phoenix.otel import register

    from .config import CFG
    register(project_name="debug-assist", endpoint=CFG.phoenix_endpoint, auto_instrument=True, verbose=False)


def _tagged(run_id: str, trace: bool) -> ExitStack:
    """Every Phoenix span of this run carries the run_id (as its session), so traces and the event log join up."""
    stack = ExitStack()
    if trace:
        from openinference.instrumentation import using_metadata, using_session
        stack.enter_context(using_session(run_id))
        stack.enter_context(using_metadata({"run_id": run_id}))
    return stack


def _summary(state: dict, run_id: str) -> str:
    from . import meter
    keys = ["outcome", "focus", "triage", "profile", "fix_clock", "repro", "attempts", "cause", "condition", "guard",
            "backtest", "approval", "published", "demo", "trace"]
    out = {k: state.get(k) for k in keys if k in state}
    m = meter.snapshot(run_id)
    if m:
        out["meter"] = {"spent_usd": m["spent_usd"], "reserved_usd": m["reserved_usd"], "cap_usd": m["cap_usd"],
                        "sandbox_s": f"{m['sandbox_used_s']} of {m['sandbox_cap_s']}", "turns": m.get("turns", {})}
    return json.dumps(out, indent=2, default=str)


def _pause(app, cfg):
    snap = app.get_state(cfg)
    intr = snap.tasks[0].interrupts[0].value if snap.tasks and snap.tasks[0].interrupts else {}
    return snap, intr


def _print_events(run_id: str) -> None:
    from . import events
    rows = events.for_run(run_id)
    if not rows:
        print(f"no events for {run_id}")
        return
    for e in rows:
        k = e["kind"]
        if k == "model_call":
            d = (f"{e['model']} in={e.get('input_tokens')} out={e.get('output_tokens')} "
                 f"${e.get('cost_usd', e.get('charged_usd', 0)):.6f} (reserved ${e.get('reserved_usd', 0):.6f})"
                 + ("" if e.get("ok") else f" FAILED {e.get('error')}"))
        elif k == "sandbox":
            d = f"exit={e['exit']} {e['seconds']}s of {e['timeout_s']}s net={'on' if e['network'] else 'off'} `{e['command'][:60]}`"
        elif k == "laya":
            d = f"{e['model']} " + ", ".join(f"{q}={a.get('choice', a.get('noul'))}" for q, a in e["answers"].items())
        elif k == "attempt":
            d = f"rung {e['rung']} #{e['n']} {e['outcome']}: {e['evidence'][:80]}"
        else:
            d = json.dumps({x: y for x, y in e.items() if x not in ("run_id", "step", "kind", "at")}, default=str)
        print(f"{e['at'][11:19]}  {e['step']:15s} {k:11s} {d}")
    print(f"\n{len(rows)} events")


def _cleanup(yes: bool) -> None:
    from . import cleanup
    from .graph import build
    app = build()

    def is_finished(run_id):
        snap = app.get_state({"configurable": {"thread_id": run_id}})
        if not snap.values:
            return True, "not a pipeline run (a trial)"
        if snap.next:
            return False, f"waiting at {snap.next[0]}: resume or approval needs its code"
        return True, f"finished: {(snap.values.get('outcome') or {}).get('exit', 'done')}"
    rows = cleanup.run(is_finished, yes=yes)
    for r in rows:
        act = "DELETED" if r.get("deleted") else ("would delete" if r["delete"] else "keep")
        print(f"{act:13s} {r['run']:34s} {r['apparent_mb']:>6} MB apparent · {r['why']}")
    gone = [r for r in rows if r["delete"]]
    print(f"\n{len(gone)} of {len(rows)} code copies {'deleted' if yes else 'would be deleted'}; each one's changes "
          f"{'were' if yes else 'will be'} saved as runs/<id>/checkout.patch. Copies share disk with the base, so "
          "the real space freed is less than the apparent size." + ("" if yes else " Add --yes to delete."))


def _open_viewer(run_id: str, port: int) -> None:
    """The run's live page on localhost, opened before the first step so every step shows up as it happens."""
    import webbrowser
    from . import server
    from .config import CFG
    (CFG.runs_dir / run_id).mkdir(parents=True, exist_ok=True)  # the page answers before the first step makes it
    if server.ensure(port):
        webbrowser.open(server.url(run_id, port))
        print(f"run viewer: {server.url(run_id, port)}")
    else:
        print("run viewer: could not start (see runs/.viewer.log); the run goes on without it")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args or args[0] not in COMMANDS or (len(args) < 2 and args[0] not in ("cleanup", "serve")):
        print(__doc__)
        sys.exit(2)
    cmd, arg = args[0], (args[1] if len(args) > 1 else "")
    demo, trace = "--demo" in flags, "--no-trace" not in flags
    opts = dict(f[2:].split("=", 1) for f in flags if "=" in f)

    if cmd == "events":
        _print_events(arg)
        return
    if cmd == "cleanup":
        _cleanup(yes="--yes" in flags)
        return
    if cmd == "serve":
        from .server import PORT, serve
        serve(int(opts.get("port", PORT)))
        return
    if cmd == "view":
        from .config import CFG
        from .viewer import write
        if not (CFG.runs_dir / arg).is_dir():
            sys.exit(f"no run folder for {arg}")
        print(f"run viewer: {write(arg, CFG.runs_dir / arg, watch='--watch' in flags)}")
        return

    from langgraph.types import Command

    from . import meter
    from .budget import run_cap
    from .config import CFG
    from .github_read import parse_issue_url
    from .graph import build
    from .preflight import as_records, report, run_preflight

    app = build()
    if cmd == "run":
        _, repo, number = parse_issue_url(arg)
        run_id = f"{repo}-{number}-{datetime.now():%Y%m%d-%H%M%S}"
    else:
        run_id = arg
    cfg = {"configurable": {"thread_id": run_id}}

    if cmd == "resume":
        snap, intr = _pause(app, cfg)
        if not snap.values:
            sys.exit(f"no run {run_id}")
        if not snap.next:
            print(f"run {run_id} already finished: {snap.values.get('outcome')}")
            return
        if intr:
            print(f"run {run_id} is waiting for your approval, not crashed. Approve or reject it.")
            cmd = "status"
        else:
            arg, demo = snap.values["issue_url"], snap.values.get("demo", False)

    if cmd in {"preflight", "run", "resume"}:
        checks = run_preflight(arg, demo=demo, trace=trace)
        ok, text = report(checks, starting=(cmd != "preflight"))
        print(text)
        if cmd == "preflight" or not ok:
            sys.exit(0 if ok else 1)

    if cmd == "approve":
        from .guardrails import fingerprint
        snap, intr = _pause(app, cfg)
        if not intr:
            sys.exit(f"run {run_id} is not waiting for approval")
        from pathlib import Path
        on_disk = fingerprint(Path(intr["pr_body_path"]).read_text())
        if on_disk != intr["sha256"]:
            sys.exit(f"REFUSED: PR.md changed since the pause (now sha256 {on_disk[:12]}, shown {intr['sha256'][:12]}).\n"
                     "Approval binds to the text you were shown. Restore it, or reject and re-run.")
        print(f"Approving sha256 {intr['sha256'][:12]}: the exact text in {intr['pr_body_path']}")

    if cmd in {"run", "resume"} and "--no-view" not in flags:
        _open_viewer(run_id, int(opts.get("port", 8777)))
    if trace and cmd in {"run", "resume", "approve", "reject"}:
        _tracing()
    with _tagged(run_id, trace):
        if cmd == "run":
            meter.open_run(run_id, run_cap({"demo": demo}), CFG.sandbox_budget_s)
            app.invoke({"run_id": run_id, "issue_url": arg, "log": [], "demo": demo, "trace": trace,
                        "preflight": as_records(checks), "focus": opts.get("focus", ""),
                        "focus_heading": opts.get("focus-heading", "")}, cfg)
        elif cmd == "resume":
            meter.open_run(run_id, run_cap({"demo": demo}), CFG.sandbox_budget_s)  # no-op if it exists (runs from before Thursday get one)
            print(f"resuming {run_id} from after its last finished step")
            app.invoke(None, cfg)
        elif cmd in {"approve", "reject"}:
            app.invoke(Command(resume="go" if cmd == "approve" else "reject"), cfg)

    snap, intr = _pause(app, cfg)
    print("\n".join(snap.values.get("log", [])))
    print(_summary(snap.values, run_id))
    if cmd == "approve" and snap.values.get("approval", {}).get("status") == "APPROVED":
        print(f"\nAPPROVED sha256 {snap.values['approval']['sha256'][:12]} (matches the text you read)")
    if intr:
        print(f"\nPAUSED at {snap.next[0]}. Read {intr.get('pr_body_path')} (sha256 {str(intr.get('sha256'))[:12]})")
        print(f"Approve: uv run debug-assist approve {run_id}    Reject: uv run debug-assist reject {run_id}")
    elif snap.values.get("outcome"):
        o = snap.values["outcome"]
        print(f"\nSTOPPED: {o['exit']}: {o['why']}")
    elif snap.next:
        print(f"\nINTERRUPTED before {snap.next[0]}. Continue: uv run debug-assist resume {run_id}")
    print(f"\nrun_id: {run_id}   events: uv run debug-assist events {run_id}   traces: http://localhost:6006")
