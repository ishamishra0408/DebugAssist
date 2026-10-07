"""Issue text exactly as the fine-tuned triage model saw it in training.

The model was trained on cleaned text (training/build_dataset.py: clean). Feeding it raw issue text at run time would
measure a different input than the one we evaluated (metric-design review, 2026-10-06). tests/test_issue_text.py
checks this copy stays identical to the training one.
"""
import re

BRACKET_TAG = re.compile(r"^\s*(\[[^\]]{1,30}\]\s*:?\s*)+")
COMMIT_PREFIX = re.compile(r"^\s*[a-z]+(\([^)]*\))?!?:\s+", re.I)
HEADING = re.compile(r"^\s*#{1,6}\s.*$", re.M)
COMMENT = re.compile(r"<!--.*?-->", re.S)
CHECKBOX = re.compile(r"^\s*[-*]\s*\[[ xX]\].*$", re.M)
SECRET_VALUE = re.compile(r"github_pat_[A-Za-z0-9_]{20,}|\bghp_[A-Za-z0-9]{30,}|sk-or-v1-[A-Za-z0-9]{20,}|"
                          r"sk-ant-[A-Za-z0-9_-]{20,}|\bsk-[A-Za-z0-9]{32,}|\bAKIA[0-9A-Z]{16}\b")


def clean(title: str, body: str) -> str:
    t = COMMIT_PREFIX.sub("", BRACKET_TAG.sub("", title or "")).strip()
    b = HEADING.sub("", CHECKBOX.sub("", COMMENT.sub("", body or "")))
    b = re.sub(r"\n{3,}", "\n\n", b).strip()
    return SECRET_VALUE.sub("[REDACTED-KEY]", f"{t}\n\n{b}")[:3000]


# The exact questions the triage model was fine-tuned on. Changing the wording changes what was measured.
TRIAGE_QUESTIONS = {
    "is_defect": {"type": "noul",
                  "instructions": "Is this a real defect in this repository's own code, rather than a feature "
                                  "request, a documentation request, a usage question, or a problem elsewhere?"},
    "kind": {"type": "choice", "instructions": "What kind of GitHub issue is this?",
             "criteria": {"bug": "something in the code does not work as intended",
                          "feature request": "asks for new behaviour or support",
                          "docs or question": "asks how to use it, or for documentation"}},
}
