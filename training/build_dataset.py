"""Build the triage fine-tuning dataset from 3 repos' issue history (read-only GitHub token).

Construct (metric-design m01): "is this a real defect in this repo's own code?"
  POSITIVE  bug-labelled, closed as COMPLETED, and closed by a MERGED pull request  → maintainers confirmed by fixing
  NEGATIVE  feature/enhancement, documentation or question-labelled issues          → not a defect report
  Excluded  bug-labelled issues NOT confirmed by a merged fix (the label is often just the reporter's template choice)

Rows use the typed-decisions format {state, questions, gold} so Laya's own training script can read them.
Split 70/15/15 by repo × class, seeded. A separate HARD set holds the scouts' hand-verified open bugs.

Run: uv run python build_dataset.py
"""
import json
import os
import random
import re
import time
import urllib.request
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
TOKEN = os.environ["GITHUB_TOKEN_READONLY"]
OUT = Path(__file__).parent / "data"
PER_CLASS_PER_REPO = 300
SEED = 7

REPOS = {
    "langchain-ai/langchain": {"pos": ["bug"], "neg": ["feature request", "documentation", "help wanted"]},
    "vercel/ai": {"pos": ["bug"], "neg": ["feature", "documentation"]},
    "Arize-ai/phoenix": {"pos": ["bug"], "neg": ["enhancement", "documentation", "question"]},
}
NEG_KIND = {"feature request": "feature request", "feature": "feature request", "enhancement": "feature request",
            "documentation": "docs or question", "question": "docs or question", "help wanted": "docs or question"}

# Scout-verified open bugs (reproduced on master or traced to an introducing PR). Never trained on.
HARD = [("vercel/ai", 21439), ("vercel/ai", 21207), ("langchain-ai/langchain", 39820),
        ("langchain-ai/langchain", 40136), ("langchain-ai/langchain", 39677), ("Arize-ai/phoenix", 16768),
        ("Arize-ai/phoenix", 16346), ("Arize-ai/phoenix", 15685)]

QUESTIONS = {
    "is_defect": {"type": "noul",
                  "instructions": "Is this a real defect in this repository's own code, rather than a feature "
                                  "request, a documentation request, a usage question, or a problem elsewhere?"},
    "kind": {"type": "choice", "instructions": "What kind of GitHub issue is this?",
             "criteria": {"bug": "something in the code does not work as intended",
                          "feature request": "asks for new behaviour or support",
                          "docs or question": "asks how to use it, or for documentation"}},
}

GQL = """query($q:String!,$cursor:String){search(query:$q,type:ISSUE,first:50,after:$cursor){
 pageInfo{hasNextPage endCursor}
 nodes{... on Issue{number title body url stateReason labels(first:15){nodes{name}}
   closedByPullRequestsReferences(first:5,includeClosedPrs:false){nodes{merged}}}}}}"""


def gql(q: str, cursor=None):
    body = json.dumps({"query": GQL, "variables": {"q": q, "cursor": cursor}}).encode()
    req = urllib.request.Request("https://api.github.com/graphql", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.load(r)
            if "errors" in d:
                raise RuntimeError(d["errors"][0].get("message"))
            return d["data"]["search"]
        except Exception as e:  # transient: back off; persistent: surface
            if attempt == 3:
                raise
            print(f"   retry after {type(e).__name__}: {e}")
            time.sleep(5 * (attempt + 1))


BRACKET_TAG = re.compile(r"^\s*(\[[^\]]{1,30}\]\s*:?\s*)+")             # "[BUG] ", "[Feature]: "
COMMIT_PREFIX = re.compile(r"^\s*[a-z]+(\([^)]*\))?!?:\s+", re.I)       # "fix(core): ", "feat: ", "docs: "
HEADING = re.compile(r"^\s*#{1,6}\s.*$", re.M)
COMMENT = re.compile(r"<!--.*?-->", re.S)
CHECKBOX = re.compile(r"^\s*[-*]\s*\[[ xX]\].*$", re.M)                  # "- [x] This is a bug, not a usage question."
MIN_CHARS = 40


SECRET_VALUE = re.compile(r"github_pat_[A-Za-z0-9_]{20,}|\bghp_[A-Za-z0-9]{30,}|sk-or-v1-[A-Za-z0-9]{20,}|"
                          r"sk-ant-[A-Za-z0-9_-]{20,}|\bsk-[A-Za-z0-9]{32,}|\bAKIA[0-9A-Z]{16}\b")


def clean(title: str, body: str) -> str:
    """Strip template scaffolding so the model learns from content, not from the template the reporter picked.
    Critical: langchain's template contains a ticked checkbox that STATES the label ("This is a bug, not a usage
    question"), so leaving checkboxes in leaks the answer (metric-design review, 2026-10-06)."""
    t = COMMIT_PREFIX.sub("", BRACKET_TAG.sub("", title or "")).strip()
    b = HEADING.sub("", CHECKBOX.sub("", COMMENT.sub("", body or "")))
    b = re.sub(r"\n{3,}", "\n\n", b).strip()
    return SECRET_VALUE.sub("[REDACTED-KEY]", f"{t}\n\n{b}")[:3000]  # public issues sometimes contain pasted keys


def dedupe_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower())[:200]


BUGLIKE = re.compile(r"\b(error|exception|traceback|doesn'?t work|not working|broken|fails?|crash(es)?)\b", re.I)


def fetch(repo: str, label: str, positive: bool, want: int) -> list[dict]:
    q = f'repo:{repo} is:issue label:"{label}"' + (" is:closed reason:completed" if positive else "")
    out, cursor = [], None
    while len(out) < want:
        page = gql(q, cursor)
        for n in page["nodes"]:
            if not n:
                continue
            if positive and not any(p and p.get("merged") for p in n["closedByPullRequestsReferences"]["nodes"]):
                continue  # bug label alone is not confirmation
            labels = {l["name"] for l in n["labels"]["nodes"]}
            if positive and labels & set(NEG_KIND):
                continue  # ambiguous: carries both kinds of label
            if not positive and "bug" in labels:
                continue
            out.append({"repo": repo, "number": n["number"], "url": n["url"], "text": clean(n["title"], n["body"]),
                        "label": "bug" if positive else NEG_KIND[label]})
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
        time.sleep(0.5)
    return out[:want]


def to_row(ex: dict) -> dict:
    bug = ex["label"] == "bug"
    gold = {"is_defect": {"probabilities": {"true": 1.0 if bug else 0.0, "false": 0.0 if bug else 1.0}},
            "kind": {"probabilities": {k: 1.0 if k == ex["label"] else 0.0 for k in QUESTIONS["kind"]["criteria"]}}}
    return {"id": f"{ex['repo']}#{ex['number']}", "url": ex["url"], "repo": ex["repo"], "label": ex["label"],
            "state": json.dumps(ex["text"]), "questions": json.dumps(QUESTIONS), "gold": json.dumps(gold)}


def get_issue_rest(repo: str, number: int) -> dict:
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/issues/{number}",
                                 headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    return {"repo": repo, "number": number, "url": d["html_url"], "text": clean(d["title"], d.get("body")),
            "label": "bug"}


def main():
    OUT.mkdir(exist_ok=True)
    rng = random.Random(SEED)
    hard_ids = {f"{r}#{n}" for r, n in HARD}
    buckets, seen, seen_text = {}, set(), set()
    dropped = Counter()
    hard_negs = []
    for repo, spec in REPOS.items():
        print(f"== {repo}")
        pos = fetch(repo, spec["pos"][0], True, PER_CLASS_PER_REPO)
        print(f"   confirmed bugs: {len(pos)}")
        negs = []
        per_neg = PER_CLASS_PER_REPO // len(spec["neg"]) + 1
        for lab in spec["neg"]:
            got = fetch(repo, lab, False, per_neg)
            print(f"   {lab}: {len(got)}")
            negs += got
        # Hard negatives: non-bugs that READ like bugs (model-independent text rule), held out from all splits.
        lookalikes = sorted([e for e in negs if BUGLIKE.search(e["text"])], key=lambda e: e["number"])
        picked = random.Random(f"{SEED}-{repo}").sample(lookalikes, min(3, len(lookalikes)))
        hard_negs += picked
        picked_ids = {e["number"] for e in picked}
        for ex in pos + negs:
            key = f"{ex['repo']}#{ex['number']}"
            tkey = dedupe_key(ex["text"])
            if key in hard_ids or ex["number"] in picked_ids:
                continue
            if key in seen:  # an issue with two labels goes to one class only
                dropped["two labels"] += 1
                continue
            if len(ex["text"].strip()) < MIN_CHARS:
                dropped["empty after cleaning"] += 1
                continue
            if tkey in seen_text:  # same opening text as another issue (template residue or duplicate)
                dropped["near-duplicate"] += 1
                continue
            seen.add(key)
            seen_text.add(tkey)
            buckets.setdefault((repo, ex["label"]), {})[ex["number"]] = ex
    print(f"dropped: {dict(dropped)}")

    splits = {"train": [], "val": [], "test": []}
    for key, by_num in sorted(buckets.items()):
        exs = sorted(by_num.values(), key=lambda e: e["number"])
        rng.shuffle(exs)
        n = len(exs)
        a, b = int(n * 0.70), int(n * 0.85)
        splits["train"] += exs[:a]
        splits["val"] += exs[a:b]
        splits["test"] += exs[b:]
    splits["hard"] = [get_issue_rest(r, n) for r, n in HARD] + hard_negs  # 8 verified bugs + look-alike non-bugs

    for name, exs in splits.items():
        with open(OUT / f"{name}.jsonl", "w") as f:
            for ex in exs:
                f.write(json.dumps(to_row(ex)) + "\n")
        print(f"{name:5s} {len(exs):5d}  {dict(Counter(e['label'] for e in exs))}")
    leak = {r["id"] for s in ("train", "val") for r in map(to_row, splits[s])} & \
           {r["id"] for r in map(to_row, splits["test"] + splits["hard"])}
    assert not leak, f"split leakage: {sorted(leak)[:5]}"
    print("no overlap between train/val and test/hard ✓")


if __name__ == "__main__":
    main()
