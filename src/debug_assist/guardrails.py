"""Guardrails that live in code, not prompts ("prose is not a control").

1. Approval attestation: your go-word is bound to a fingerprint of the exact PR text; publishing refuses any other text.
2. No names in public output: deterministic check, before anything is published.
3. Condition frozen before the guard: the second story's condition is fingerprinted first; the back-test refuses a
   guard that predates it or a condition that changed after.
(4. No secrets in the sandbox lives in sandbox.py. 5. The spend cap lives in budget.py.)

`store` is any object with insert_one/find_one (a MongoDB collection, or a fake in tests).
"""
import hashlib
import re
from datetime import datetime, timezone


class GuardrailViolation(RuntimeError):
    pass


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 1 ── approval attestation ──────────────────────────────────────────────────────────────────────
def record_approval(store, run_id: str, pr_body: str, approver: str) -> dict:
    doc = {"kind": "approval", "run_id": run_id, "sha256": fingerprint(pr_body), "approver": approver, "at": now()}
    store.insert_one(dict(doc))
    return doc


def verify_approval(store, run_id: str, pr_body: str) -> dict:
    doc = store.find_one({"kind": "approval", "run_id": run_id})
    if not doc:
        raise GuardrailViolation(f"run {run_id}: no approval on record; nothing is published without your go-word")
    if doc["sha256"] != fingerprint(pr_body):
        raise GuardrailViolation(f"run {run_id}: PR text changed after approval; re-approve the new text")
    return doc


# 2 ── no names in public output ─────────────────────────────────────────────────────────────────
HANDLE = re.compile(r"(?<![\w@./])@[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\b(?!\.\w|\()")
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)


def find_names(text: str, names: list[str]) -> list[str]:
    """Return every @handle (outside code, where GitHub doesn't treat them as mentions) and every known name
    (e.g. git-blame authors, checked everywhere, code included) found in text."""
    hits = HANDLE.findall(CODE.sub(" ", text))
    for n in names:
        if n and re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text, re.IGNORECASE):
            hits.append(n)
    return sorted(set(hits))


def assert_no_names(text: str, names: list[str]) -> None:
    hits = find_names(text, names)
    if hits:
        raise GuardrailViolation(f"public text names people: {hits}; describe conditions, not people")


# 3 ── condition frozen before the guard ─────────────────────────────────────────────────────────
def freeze_condition(store, run_id: str, condition: str) -> dict:
    """Idempotent for the SAME text (a resumed run re-runs the step), refuses a different one."""
    prior = store.find_one({"kind": "condition_freeze", "run_id": run_id})
    if prior:
        if prior["sha256"] != fingerprint(condition):
            raise GuardrailViolation(f"run {run_id}: condition already frozen; a different one cannot replace it")
        return {k: prior[k] for k in ("kind", "run_id", "sha256", "frozen_at")}
    doc = {"kind": "condition_freeze", "run_id": run_id, "sha256": fingerprint(condition), "frozen_at": now()}
    store.insert_one(dict(doc))
    return doc


def verify_condition_frozen(store, run_id: str, condition: str, guard_created_at: str) -> dict:
    doc = store.find_one({"kind": "condition_freeze", "run_id": run_id})
    if not doc:
        raise GuardrailViolation(f"run {run_id}: condition was never frozen")
    if doc["sha256"] != fingerprint(condition):
        raise GuardrailViolation(f"run {run_id}: condition changed after it was frozen")
    if guard_created_at < doc["frozen_at"]:
        raise GuardrailViolation(f"run {run_id}: guard was written before the condition was frozen")
    return doc
