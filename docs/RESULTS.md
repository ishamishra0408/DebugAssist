# Results: our fixes against the maintainers' own

Each fix DebugAssistAgent wrote, compared with the fix the vercel/ai maintainers merged or opened for the same issue.
The comparison runs on the commit the run itself used, with the network off, on a Mac's test machine (Docker).
Nothing here is posted anywhere.

## #22085: WorkflowAgent drops provider tool results when the output is cut off

Run `ai-22085-20261009-165445` (hosted, Claude Opus), on vercel/ai at `e7f55a4`.
Maintainers' fix: [#22093](https://github.com/vercel/ai/pull/22093), merged 2026-10-09, commit `7c0410d`.

| | Maintainers (#22093) | DebugAssistAgent |
|---|---|---|
| Cause | The `length` branch of `model-call-iterator.ts` drops provider-executed tool results | **Same:** lines 570–595, the `length` branch |
| Where the change is | Before that branch: a cut-off step with provider results goes down the normal tool-result path | Inside that branch: the missing results are put back by hand at their recorded positions |
| How results are inserted | The existing shared function, so cut-off steps match normal ones | Built by the fix itself (`json` / `error-text` output, metadata copied) |
| Other tool calls in the same step | Handled: only finished provider calls and broken local calls are processed; nothing runs | Not handled |
| Size | `model-call-iterator.ts` +16 −4 | `model-call-iterator.ts` +33 −4 |
| Tests | 4 new cases (+208 lines) in the existing `workflow-agent-compat.test.ts` | 1 new file, 98 lines |
| Release note | `.changeset` added | none (now added for repos that keep one) |

Each fix against each set of tests:

| Code | Our test (1 case) | Maintainers' tests (59 in the file, 4 new) |
|---|---|---|
| Unfixed (`e7f55a4`) | fails (the bug) | 4 fail, 55 pass: all 4 new cases show the bug |
| With our fix | passes | **3 fail**, 56 pass |
| With the maintainers' fix | passes | all 59 pass |

The 3 cases our fix fails: a cut-off step with a provider result **and** a broken local tool call (truncated JSON,
invalid input, an unknown tool). Our fix leaves that step with no result and no error, so the next request would still
be rejected.

How it was run: three copies of the base checkout at `e7f55a4`. Our fix was copied from the run page; our test was copied
from the run's proof and matched its fingerprint (`3e8106c6…`). The maintainers' PR was written against newer code, so
three of its four changes applied as they were and one was carried over by hand; the result is +16 −4, the same as the
PR. Their test changes applied to every copy. Each test file ran in the vercel/ai test image with the network off.

## #22543: Vue `useObject` drops headers given as a `Headers` instance

Run `ai-22543-20261010-121837` (on a Mac, Claude Opus), on vercel/ai main at `ba05943`.
Maintainers' fix: [#22545](https://github.com/vercel/ai/pull/22545), open at the time of the run.

| | Maintainers (#22545) | DebugAssistAgent |
|---|---|---|
| Cause | `use-object.ts` spreads `headers` as a plain object | **Same lines** (151–160) |
| Change | Uses the repo's shared `normalizeHeaders` and lower-cases the default content type | Wrote its own conversion and kept `'Content-Type'` capitalised |
| Content type set by a `Headers` instance | the caller's value wins | sends `"application/json, text/plain"`: **fails** |
| Tests | cases added to `use-object.ui.test.tsx`, plus a changeset | two new test files |

With our fix, the whole vue suite passes (58 tests), and so does the plain-object override case; the `Headers` override
case fails. Found by an independent review of the run (`design/review-run-22543.md` in the work folder).

## What changed because of these

- The fix step now sees how other packages and the repo's own helpers do the same thing, and fix PRs named in the issue's
  discussion, and is told to reuse the existing path and cover every form of the input.
- The second test and the guard are told to include input that also sets what the code sets by default.
- A pull request carries one test, named after the file it tests, and a changeset where the repo keeps them.

## What is next

- Run the upstream fix's tests against every fix before your OK, and show the result on the page.
- A replay benchmark: closed issues with merged fixes, run from the commit before the merge with the fix hidden, scored
  on cause, the maintainers' tests, and size.
