# Operations: the long version

Setup, commands, how a run ends, the guardrails, and what broke and how it was fixed. The short version is the
[README](../README.md); hosting is [`deploy/DEPLOY.md`](../deploy/DEPLOY.md).

## Setup on a Mac (first done 2026-10-06)

| Piece | How it runs |
|---|---|
| Python 3.12 **native arm64** (uv-managed) | `.python-version` pins it. MLX and Laya need arm64; Intel Pythons under Rosetta will not do |
| LangGraph, LangChain (OpenRouter), MongoDB checkpointer, Phoenix OTel, laya-mlx | `uv sync` |
| MongoDB Atlas Local (vector search) + Arize Phoenix | `docker compose up -d` (localhost only) |
| Qwen3-Embedding 0.6B | `ollama pull qwen3-embedding:0.6b` |
| Laya checkpoint (MLX, Apache-2.0, 0.85 GB) | downloads on first load |
| Pre-commit hook | `git config core.hooksPath .githooks` once per clone: a commit is refused while any test fails |

## Every session

```bash
cd ~/Projects/DebugAssist
docker compose up -d                                    # MongoDB + Phoenix (UI: http://localhost:6006)
python3 scripts/doctor.py                               # works even if the package won't import
uv run debug-assist preflight <issue-url>               # every dependency, named PASS/FAIL, starts nothing
uv run debug-assist serve --port=8777                   # the web app (stops after 30 min without a request)
```

`run` and `resume` always run the preflight first and refuse to start, listing every failing check. `--demo` uses Claude
Opus; `--no-trace` runs without Phoenix on purpose (recorded as trace OFF).

## Commands

```bash
uv run debug-assist run <issue-url> [--focus=TEXT | --focus-heading=HEADING] [--look-in=PATH,PATH] [--test-in=PATH]
uv run debug-assist resume  <run-id>                  # after a crash: continues after the last finished step
uv run debug-assist answer  <run-id> yes|no           # install a missing package?
uv run debug-assist answer  <run-id> advisor|run      # an advisor disagreed: whose plan to follow
uv run debug-assist approve <run-id>                  # your go-word; prints the fingerprint it approves
uv run debug-assist reject  <run-id>
uv run debug-assist events  <run-id>                  # every model call, sandbox command, decision, attempt
uv run debug-assist connect <repo-url>                # connect a repo: its setup, its test sandbox, its tests proven
uv run debug-assist cleanup [--yes]                   # delete finished runs' code copies (patch kept)
uv run pytest                                         # 315 tests (Mongo and Docker ones skip if those are down)
```

`--focus` / `--focus-heading` pick the ONE problem to reproduce. Without either, the home page picks it from the issue:
a section that states the problem and quotes code, else the issue's opening description, else the title.

## Base checkouts (one per repo, prepared once)

Each run copies a clean, installed, built checkout of the repo (`~/Projects/checkouts/base/<owner>-<repo>@<sha7>`;
APFS copy-on-write, about 20 s). The run's copy is then moved to main's newest commit: if the package list changed, the
copy is installed again (on a Mac) or the E2B template is rebuilt once (hosted); otherwise only the changed packages are
built.

```bash
uv run python scripts/prepare_base.py vercel/ai       # clone, install (network on), build (network off)
```

## How a run ends

| Outcome | When |
|---|---|
| `NEEDS PERSON` | Triage confidence below 0.6, or you went with an advisor who says a person should decide |
| `NOT A DEFECT` | Triage confident it is not a bug |
| `TEST MACHINE NOT READY` | The bug's package is not on the test machine and you said no, or its tests cannot load |
| `NEVER REPRODUCED` | The reproduction ladder used its attempts and no test failed for the issue's reason |
| `CAUSE NOT FOUND` | No real source file and lines could be named |
| `FIX NOT VALIDATED` | No attempt turned the failing tests green with every affected suite passing, or the second test still fails |
| `TEST FLAWED` | The fixer says the judging test cannot be passed (it may never edit it); a person checks the test |
| `STORY NOT WRITTEN` / `GUARD NOT WRITTEN` | No report passed the checks (names, 2+ conditions, nothing invented), or no guard failed on the old code |
| paused | Waiting for you: install a package, an advisor disagreed, or Your OK |
| `REJECTED` / `READY FOR YOU TO PUBLISH` | After your answer at Your OK |

## Guardrails (code, not prompts)

| Guardrail | Where | Refuses |
|---|---|---|
| Approval fingerprint | `guardrails.py` | Publishing without your go-word, or with the text changed after it |
| Read-only GitHub | `github_read.py` | Any write: the pipeline has no write credential |
| Sign-in | `ghauth.py`, `owners.py` | Accounts not on the allow-list; another person's runs; spend past a person's monthly cap |
| No secrets in the sandbox | `sandbox.py`, `sandbox_e2b.py` | Host variables inside the test machine; network off by default |
| Install only on your yes | `pkgcheck.py` | Adding a package to the test machine without asking |
| Advisors' answers | `graph.py` | Anything from the advisors' server reaching a prompt unless you chose its plan, and then only our own repo files and our own report's sentences |
| Spend stop that survives a crash | `meter.py`, `budget.py` | A call whose worst case could pass the run cap (checked before the call, atomically in MongoDB); calls past a step's turn cap |
| One door to the model | `models.py` | Any generation call that skips the meter |
| Reproduction | `ladder.py`, `testwriter.py` | Writing a fix before a test failed; a failure that does not show the issue (a crash, a wrong reason, or only the test's own message saying so) |
| Validated fix only | `fixer.py` | A fix that edits tests or fixtures; a SEARCH that matches 0 or 2+ places; a fix that leaves a judging test red or breaks an affected suite |
| Pipe exit codes | `sandbox.py` | `tests \| tail` hiding a failure: every command runs under `bash -o pipefail` |
| No names in public text | `guardrails.py` | @handles outside code, and known names anywhere |
| Condition frozen first | `guardrails.py` | A guard older than the condition, or a condition edited after freezing |

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No module named 'debug_assist'` | iCloud set the hidden flag on `.pth` files; Python 3.12 skips hidden `.pth` | `python3 scripts/doctor.py --fix`; keep the repo out of iCloud |
| Mongo `NotPrimaryError` / "not a member of" replica set | Atlas Local names its replica set after the container hostname | The hostname is pinned in `docker-compose.yml`; never remove that line |
| Mongo crash-loops "Unable to acquire security key" | The replica-set key lived in an anonymous volume | Now a named volume (`mongo-config`); never remove it |
| `Index conditions_vec not initialized` | The vector index is rebuilding after a restart | The run waits up to 90 s, then reports UNEVALUABLE (never "no siblings") |
| Sandbox tests skipped | Docker Desktop paused | Whale menu ▸ Resume, then `docker compose up -d` |
| `INTERRUPTED before <step>` | The run died mid-way (laptop slept, provider error, Ctrl-C) | `uv run debug-assist resume <run-id>`; spend, turns and attempts are in MongoDB, nothing is counted twice |
| `BudgetExceeded ... Refused BEFORE calling` | The next call's worst case would pass the run cap | Working as intended; `events <run-id>` shows where the money went |
| The local app stopped | It stops after 30 minutes without a request | `uv run debug-assist serve --port=8777` |

## Secrets

Copy `.env.example` → `.env` and fill it in yourself. `.env` is git-ignored. The pipeline holds **read-only** GitHub
access only; publishing a PR is a step you take yourself after your go-word.
