"""The pipeline must feed the triage model the same text and questions it was trained and measured on."""
import importlib.util
import json
from pathlib import Path

from debug_assist import issue_text

TRAINING = Path(__file__).resolve().parents[1] / "training"


def _training_module():
    spec = importlib.util.spec_from_file_location("build_dataset", TRAINING / "build_dataset.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SAMPLES = [
    ("[BUG]: grep fails on /app/", "- [x] This is a bug, not a usage question.\n### Description\nIt fails.\n<!-- hi -->"),
    ("fix(core): dumps() TypeError", "Traceback...\n\n\n\nmore"),
    ("feat: add Gemini", "key sk-or-v1-abcdefghijklmnopqrstuvwxyz0123 pasted by mistake"),
    ("", ""),
]


def test_clean_matches_training():
    train_clean = _training_module().clean
    for title, body in SAMPLES:
        assert issue_text.clean(title, body) == train_clean(title, body)


def test_questions_match_training():
    assert json.dumps(issue_text.TRIAGE_QUESTIONS, sort_keys=True) == \
        json.dumps(_training_module().QUESTIONS, sort_keys=True)


def test_label_stating_checkbox_and_keys_are_removed():
    out = issue_text.clean(*SAMPLES[0]) + issue_text.clean(*SAMPLES[2])
    assert "This is a bug" not in out and "sk-or-v1" not in out and "[REDACTED-KEY]" in out
