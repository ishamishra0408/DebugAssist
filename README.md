# Debug Assist — a learning machine for bugs

Takes a GitHub bug and returns three things: **the fix**, **why it shipped** (conditions, not blame), and **a lasting
guard** that catches this kind of bug next time. Design docs: `~/Downloads/debug-assist-pipeline/` (start with
`ARCHITECTURE.md` and `DECISIONS.md`).

## Setup (done 2026-10-06)
| Piece | How it runs |
|---|---|
| Python 3.12 **native arm64** (uv-managed) | `.python-version` pins it. The system Pythons on this Mac are Intel builds under Rosetta, and MLX/Laya need arm64 |
| LangGraph, LangChain (OpenRouter, MCP, Ollama), MongoDB checkpointer, Phoenix OTel, laya-mlx | `uv sync` |
| MongoDB Atlas Local (vector search) + Arize Phoenix | `docker compose up -d` (localhost only) |
| Qwen3-Embedding 0.6B | `ollama pull qwen3-embedding:0.6b` |
| Laya checkpoint `aac6fef/laya-mlx` (independent MLX port, Apache-2.0, 0.85 GB) | Downloads on first load |

## Every session
```bash
cd ~/Projects/DebugAssist
docker compose up -d                                                  # MongoDB + Phoenix (UI: http://localhost:6006)
python3 scripts/doctor.py                                             # layer 0: works even if the package won't import
uv run debug-assist preflight https://github.com/vercel/ai/issues/21439   # every dependency, named PASS/FAIL, starts nothing
```
`run` and `resume` always do the preflight first and refuse to start with every failing check listed. `--demo` uses Claude Opus
under the $2.50 demo cap; `--no-trace` runs without Phoenix on purpose (recorded as trace OFF).

## Run it
```bash
uv run debug-assist run https://github.com/vercel/ai/issues/21439 "--focus-heading=Secondary observation"
                                                                     # runs until it pauses for approval or stops
uv run debug-assist resume  <run-id>                                 # after a crash: continues after the last finished step
uv run debug-assist approve <run-id>                                 # your go-word; prints the sha it approves
uv run debug-assist reject  <run-id>
uv run debug-assist events  <run-id>                                 # every model call, sandbox command, decision, attempt
uv run debug-assist cleanup [--yes]                                  # delete finished runs' code copies (patch kept)
uv run pytest                                                        # 95 tests (Mongo + Docker ones skip if those are down)
```
`--focus-heading=…` picks the ONE problem to reproduce from a section of the issue (or `--focus="…"` gives it
outright); without either, the title is the focus. A missing section stops the run as NEEDS PERSON.

## Base checkouts (one per repo, prepared once)
Each run copies a clean, installed, built checkout of the repo at the commit pinned in `profiles.py`
(`~/Projects/checkouts/base/<owner>-<repo>@<sha7>`; APFS copy-on-write, ~20 s, little new disk). Preflight refuses a
run when the base is missing, at another commit, or edited.
```bash
uv run python scripts/prepare_base.py vercel/ai      # clone at the pinned commit, install (network on), build (off)
```
A run ends one of these ways (the `outcome` field, printed as `STOPPED: ...`):

| Outcome | When |
|---|---|
| `NEEDS PERSON` | Triage confidence below 0.6: a person reads the issue first |
| `NOT A DEFECT` | Triage confident it is not a bug (is_defect p < 0.5) |
| `NEVER REPRODUCED` | The reproduction ladder used its 4 attempts and nothing went red: no fix for a bug we could not see |
| `CAUSE NOT FOUND` | The model could not name a real source file and lines for the cause |
| `FIX NOT VALIDATED` | 3 fix attempts, none turned the failing test green with every affected suite still passing |
| `TEST FLAWED` | The fixer says the judging test cannot be passed (it may never edit it); a person checks the test |
| `STORY NOT WRITTEN` / `GUARD NOT WRITTEN` | No second story passed the checks (names, 2+ conditions, nothing invented), or no guard failed on the unfixed code |
| paused at approval | Waiting for your go-word |
| `REJECTED` / `READY FOR YOU TO PUBLISH` | After your answer |

Status (2026-10-07): **8 of 9 steps are real.** Only the back-test half of `test_past_bugs` is still to build (its
vector search is real). Use `--demo` (Claude Opus) for runs that matter: the dev model's fixes pass one test but miss
the issue, and only a fix confirmed by two independent tests counts toward ⏱. Real today: GitHub read, Laya
triage + typed exits, sandbox secret probe, the ladder's plan and climb logic, the spend meter, resume, the event
log, condition freeze, vector search, approval fingerprint, read-only publish path (`runs/<id>/publish.sh`).

## Guardrails (code, not prompts)
| Guardrail | Where | Refuses |
|---|---|---|
| Approval fingerprint | `guardrails.py` | Publishing without your go-word, or with text changed after it |
| Read-only GitHub | `github_read.py` | Any write: the pipeline has no write credential |
| No secrets in sandbox | `sandbox.py` | Host env vars inside the container (probe vs the image's own env); network off by default |
| Spend stop that survives a crash | `meter.py`, `budget.py` | A call whose worst case could pass the run cap (checked BEFORE the call, atomically in MongoDB); calls past a step's turn cap; models with no price on file. A crash mid-call leaves the reservation counted |
| One door to the model | `models.py` | Any generation call that skips the meter: `ChatOpenRouter` is built only in the private `_writer` (a test enforces it) |
| Sandbox time cap | `meter.py`, `sandbox.py` | Sandbox commands past `SANDBOX_BUDGET_S` (1800 s per run); the last command gets what is left |
| Reproduction ladder | `ladder.py` | Writing a fix before a test went red; more than 4 attempts; counting an unexplained failure as a reproduction; a made-up-data RED goes unconfirmed (re-run on a recorded stream when the repo has one) |
| Test writer | `testwriter.py` | A test placed anywhere but a NEW file beside the code; a "recorded stream" test that reads no fixture; a RED whose failing assertion doesn't show the focus's own strings (wrong-reason RED); env vars or real hosts in a test |
| Validated fix only | `fixer.py` | A fix that edits tests, fixtures or the failing test; a SEARCH that matches 0 or 2+ places; a fix that leaves the test red or breaks any affected suite (the changed packages + every installed package depending on them). The fix clock counts only a validated fix |
| Pipe exit codes | `sandbox.py` | `tests \| tail` hiding a failure: every command runs under `bash -o pipefail` |
| Pre-commit hook | `.githooks/pre-commit` | A commit while any test fails (`git config core.hooksPath .githooks` once per clone) |
| No names in public text | `guardrails.py` | @handles outside code, and known names anywhere |
| Condition frozen first | `guardrails.py` | A guard older than the condition, or a condition edited after freezing |

## Troubleshooting (all hit on 2026-10-06)
| Symptom | Cause | Fix |
|---|---|---|
| `No module named 'debug_assist'` | iCloud set the macOS *hidden* flag on `.pth` files; Python 3.12 skips hidden `.pth` | `python3 scripts/doctor.py --fix`. The repo moved out of iCloud to `~/Projects/DebugAssist` on 2026-10-06 |
| Mongo `NotPrimaryError` / "not a member of" replica set | Atlas Local names its replica set after the container hostname | Hostname is pinned in `docker-compose.yml`; never remove that line |
| Mongo crash-loops "Unable to acquire security key" | The replica-set key lived in an anonymous volume that `docker compose down` orphaned | Now a named volume (`mongo-config`); never remove it |
| `Index conditions_vec not initialized` | Vector index rebuilding after a restart | The pipeline waits up to 90 s, then reports UNEVALUABLE (never "no siblings") |
| `Failed to connect to Ollama` | Ollama app not running | `open -a Ollama` |
| Sandbox tests skipped | Docker Desktop paused | Whale menu ▸ Resume, then `docker compose up -d` |
| `INTERRUPTED before <step>` | The run died mid-way (laptop slept, provider error, Ctrl-C) | `uv run debug-assist resume <run-id>`. Spend, turns and ladder attempts are in MongoDB, so nothing is double-counted or repeated |
| `BudgetExceeded ... Refused BEFORE calling` | The next call's worst case would pass the run cap | Working as intended. `events <run-id>` shows where the money went |

## Secrets
Copy `.env.example` → `.env` and fill it in yourself. `.env` is git-ignored. The pipeline holds **read-only** GitHub
access only; publishing a PR happens after your go-word.
