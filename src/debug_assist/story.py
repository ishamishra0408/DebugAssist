"""why_it_shipped: the second story (conditions, never people), the way the by-hand run of vercel/ai #21439 told it.

Code gathers the evidence, read-only from GitHub, starting from the lines the validated fix removed:
  written    the oldest commit whose version of the file contains the line (binary search over the file's history)
  shaped     every later commit whose diff touches the line, with its PR: title, merge date, description, how many
             reviews and review comments, and whether it changed any test
  released   the CHANGELOG heading that first lists the PR (from the run's own checkout)
  reported   the issue: when, and what its labels say (e.g. the repo's own bot could not reproduce it)
The model writes the story from that evidence alone, and code checks it:
  - no names: the model never sees a login, @handles in PR text are blanked, and the result is checked against every
    author, reviewer and the reporter (guardrails.assert_no_names)
  - at least two conditions, and the one named CONDITION for the corpus
  - every #number it cites must be in the evidence: nothing invented
"""
import base64
import re
from pathlib import Path

from .github_read import api
from .guardrails import GuardrailViolation, find_names
from .models import write

TOUCHING_CAP = 60   # newest commits of the file whose diffs are read
PR_CAP = 6          # PRs described in detail


class StoryRefused(ValueError):
    pass


def signature_lines(patch: str) -> list[str]:
    """The code the fix removed (minus blank and comment-only lines): the line(s) the story is about."""
    out = []
    for l in patch.splitlines():
        if l.startswith("-") and not l.startswith("---"):
            t = l[1:].strip()
            if t and not t.startswith(("//", "#", "*", "/*")) and t not in out:
                out.append(t)
    return out


def blank_handles(text: str) -> str:
    return re.sub(r"(?<![\w@./])@[A-Za-z0-9][A-Za-z0-9-]{0,38}\b", "@someone", text or "")


def file_history(owner: str, repo: str, path: str) -> list[dict]:
    """Newest first, up to 300 commits that touched the path (renames are not followed: a stopping point)."""
    out = []
    for page in (1, 2, 3):
        got = api(f"repos/{owner}/{repo}/commits?path={path}&per_page=100&page={page}") or []
        out += [{"sha": c["sha"], "date": c["commit"]["committer"]["date"][:10],
                 "title": c["commit"]["message"].splitlines()[0][:120]} for c in got]
        if len(got) < 100:
            break
    return out


def file_at(owner: str, repo: str, path: str, sha: str) -> str | None:
    d = api(f"repos/{owner}/{repo}/contents/{path}?ref={sha}")
    if not d or "content" not in d:
        return None
    return base64.b64decode(d["content"]).decode(errors="ignore")


def introduced(owner: str, repo: str, path: str, history: list[dict], needle: str) -> dict | None:
    """The oldest commit in the file's history whose version contains `needle` (binary search; history is newest
    first). None if even the newest version lacks it."""
    has = lambda i: needle in (file_at(owner, repo, path, history[i]["sha"]) or "")
    if not history or not has(0):
        return None
    lo, hi = 0, len(history) - 1          # invariant: history[lo] has it
    if has(hi):
        return {**history[hi], "at_history_start": True}
    while hi - lo > 1:                    # history[hi] lacks it
        mid = (lo + hi) // 2
        lo, hi = (mid, hi) if has(mid) else (lo, mid)
    return history[lo]


def touched(owner: str, repo: str, sha: str, needle: str, path: str) -> bool:
    c = api(f"repos/{owner}/{repo}/commits/{sha}") or {}
    for f in c.get("files", []):
        if f.get("filename") == path and any(needle in l for l in (f.get("patch") or "").splitlines()
                                             if l.startswith(("+", "-"))):
            return True
    return False


def pr_for(owner: str, repo: str, sha: str) -> dict | None:
    prs = [p for p in (api(f"repos/{owner}/{repo}/commits/{sha}/pulls") or []) if p.get("merged_at")]
    if not prs:
        return None
    p = prs[0]
    reviews = api(f"repos/{owner}/{repo}/pulls/{p['number']}/reviews?per_page=100") or []
    comments = api(f"repos/{owner}/{repo}/pulls/{p['number']}/comments?per_page=100") or []
    files = api(f"repos/{owner}/{repo}/pulls/{p['number']}/files?per_page=100") or []
    names = {p["user"]["login"]} | {r["user"]["login"] for r in reviews if r.get("user")} | \
            {c["user"]["login"] for c in comments if c.get("user")}
    return {"number": p["number"], "title": p["title"], "merged": p["merged_at"][:10],
            "opened": p["created_at"][:10], "description": blank_handles((p.get("body") or "")[:900]),
            "reviews": len(reviews), "approvals": sum(r.get("state") == "APPROVED" for r in reviews),
            "review_comments": len(comments),
            "tests_changed": [f["filename"] for f in files if re.search(r"\.test\.|/tests?/|_test\.", f["filename"])][:8],
            "_names": sorted(names)}


def release_of(checkout: Path, pkg_dir: str, number: int, sha: str) -> str | None:
    log = Path(checkout) / "packages" / pkg_dir / "CHANGELOG.md"
    if not log.exists():
        return None
    heading, found = None, None
    for line in log.read_text(errors="ignore").splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
        if heading and (f"#{number}" in line or sha[:7] in line):
            found = heading  # changelogs are newest first: keep going to the OLDEST mention
    return found


def gather(issue: dict, checkout: Path, cause_file: str, fix_patch: str) -> dict:
    owner, repo, path = issue["owner"], issue["repo"], cause_file
    sig = signature_lines(fix_patch)
    needle = sig[0] if sig else ""
    ev = {"line": needle, "file": path, "issue": {"number": issue["number"], "opened": (issue.get("created_at") or "")[:10],
                                                  "labels": issue.get("labels", []), "comments": issue.get("comments", 0)},
          "written": None, "shaped": [], "stops": [], "_names": {issue.get("reporter", "")}}
    if not needle:
        ev["stops"].append("the fix removed no line, so there is no line whose history to trace")
        return ev
    history = file_history(owner, repo, path)
    origin = introduced(owner, repo, path, history, needle)
    if not origin:
        ev["stops"].append(f"`{needle}` is not in the newest version of {path} on GitHub")
        return ev
    if origin.get("at_history_start"):
        ev["stops"].append(f"the line is already in the oldest version of {path} GitHub lists under this path "
                           "(the file was renamed or moved; earlier history was not followed)")
    newer = history[:[h["sha"] for h in history].index(origin["sha"])]  # commits after the line was written
    shaping = [h for h in reversed(newer[:TOUCHING_CAP]) if touched(owner, repo, h["sha"], needle, path)]
    if len(newer) > TOUCHING_CAP:
        ev["stops"].append(f"only the newest {TOUCHING_CAP} of {len(newer)} later commits were read for changes to the line")
    pkg_dir = path.split("/")[1]
    for i, c in enumerate([origin] + shaping):
        pr = pr_for(owner, repo, c["sha"]) if i < PR_CAP else None
        entry = {"role": "written" if i == 0 else "changed the line", "commit": c["sha"][:10], "date": c["date"],
                 "title": c["title"]}
        if pr:
            ev["_names"] |= set(pr.pop("_names"))
            entry |= {"pr": pr, "released": release_of(checkout, pkg_dir, pr["number"], c["sha"])}
        if i == 0:
            ev["written"] = entry
        else:
            ev["shaped"].append(entry)
    if len(shaping) + 1 > PR_CAP:
        ev["stops"].append(f"PR details were read for the first {PR_CAP} commits only")
    ev["stops"].append("only the file the fix changed was traced; shared code it calls was not")
    return ev


SYSTEM = """You write the SECOND STORY of a bug: how it came to ship, told through conditions, never people.
Use ONLY the evidence given. Do not invent PRs, dates, versions or reviews. Never name or describe a person.
Write markdown with exactly these sections:
## Critical junctures
A table: | Stage | When | Change | What was known then | What the tests covered |  (stages: written, changed,
released, reported; use the PR descriptions for "what was known")
## Conditions that only together let it ship
At least two conditions, each starting "C1 —", "C2 —", …, each saying why it was necessary (what would have caught
the bug had it been absent). Then one line saying which together let it ship and which let it stay unreported.
## Where the analysis stopped
Bullets: each stopping point given, and what it would take to go further.
Then a final line:  CONDITION: <one sentence naming the condition that let this class of bug ship, in plain words>"""


def messages(focus: str, cause: dict, fix_patch: str, ev: dict, refusal: str = "") -> list:
    import json
    public = {k: v for k, v in ev.items() if not k.startswith("_")}
    user = f"""THE BUG (fixed and validated): {focus}
CAUSE: {cause['file']} lines {cause['lines'][0]}-{cause['lines'][1]}. {cause.get('why', '')}
THE FIX:
{fix_patch[:2500]}

EVIDENCE (read-only GitHub and the repo's changelog; the line traced is `{ev['line']}`):
{json.dumps(public, indent=1)[:12000]}"""
    if refusal:
        user += f"\n\nYOUR LAST STORY WAS REFUSED: {refusal}. Write it again."
    return [("system", SYSTEM), ("user", user)]


def check(story: str, ev: dict, names: set) -> str:
    """Return the named condition, or raise StoryRefused / GuardrailViolation."""
    hits = find_names(story, sorted(n for n in names if n))
    if hits:
        raise GuardrailViolation(f"the story names people: {hits}")
    if len(re.findall(r"(?m)^\s*(?:[-*|]\s*)?\**C\d+\b", story)) < 2:
        raise StoryRefused("fewer than two conditions (C1 —, C2 —)")
    allowed = {ev["issue"]["number"]} | {e["pr"]["number"] for e in [ev.get("written") or {}] + ev["shaped"] if e.get("pr")}
    cited = {int(n) for n in re.findall(r"#(\d{2,6})\b", story)}
    invented = sorted(cited - allowed)
    if invented:
        raise StoryRefused(f"cites #{', #'.join(map(str, invented))}, which is not in the evidence")
    m = re.search(r"^\s*CONDITION:\s*(.+)$", story, re.M)
    if not m:
        raise StoryRefused("no CONDITION: line")
    return m.group(1).strip()


def tell(state: dict, checkout: Path, cause: dict, fix_patch: str) -> dict:
    ev = gather(state["issue"], checkout, cause["file"], fix_patch)
    names = set(ev["_names"])
    refusal = ""
    for _ in range(2):  # the why_it_shipped turn cap is 2
        msg, _ = write(state, "why_it_shipped", messages(state.get("focus") or state["issue"]["title"], cause,
                                                         fix_patch, ev, refusal), max_tokens=3500)
        story = str(msg.content).strip()
        try:
            condition = check(story, ev, names)
            body = re.sub(r"^\s*CONDITION:.*$", "", story, flags=re.M).strip()
            return {"text": body, "condition": condition, "evidence": {k: v for k, v in ev.items() if not k.startswith("_")},
                    "names_checked": len(names)}
        except (StoryRefused, GuardrailViolation) as e:
            refusal = str(e)
    raise StoryRefused(f"no acceptable story after 2 tries: {refusal}")
