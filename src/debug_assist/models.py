"""The three model roles: Laya decides, Claude/open-weight writes, Qwen3 embeds.

write() is the ONLY door to a generation model (a test checks that ChatOpenRouter appears nowhere else). Every call
through it: turn cap (MongoDB) → worst-case reservation, refused BEFORE the call if it could pass the run cap →
the call → actual cost settled → one event row.
"""
import time
import warnings
from functools import lru_cache

from . import events, meter
from .budget import cost_usd, worst_case_usd
from .config import CFG


@lru_cache(maxsize=2)
def laya(which: str = "general"):
    """'triage': fine-tuned on our issue data, used ONLY for the questions it was trained on.
    'general': original Laya, for every other decision, until those are measured (DECISIONS: option B)."""
    import laya_mlx

    path = CFG.laya_triage_checkpoint if which == "triage" else CFG.laya_checkpoint
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # known: some choice buckets with 11+ options are uncalibrated
        return laya_mlx.load(path)


def decide(state_text: str, questions: dict, which: str = "general") -> dict:
    """Typed decisions with calibrated probabilities (local, milliseconds, $0). Hosted: the same Laya, on the Mac."""
    t0 = time.monotonic()
    answers = (_remote_decide(state_text, questions, which) if CFG.laya_url
               else laya(which).predict(state_text, questions)["answers"])
    events.log("laya", model=which, ms=round((time.monotonic() - t0) * 1000),
               answers={q: {k: a[k] for k in ("choice", "noul", "answer_confidence") if k in a}
                        for q, a in answers.items()})
    return answers


def _remote_decide(state_text: str, questions: dict, which: str) -> dict:
    """Ask laya_server.py on the Mac (through the tunnel). The shared secret goes in a header, never in the URL."""
    import json
    import urllib.request
    req = urllib.request.Request(f"{CFG.laya_url}/decide", method="POST",
                                 data=json.dumps({"text": state_text, "questions": questions, "which": which}).encode(),
                                 headers={"Content-Type": "application/json", "X-Laya-Token": CFG.laya_token})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["answers"]


@lru_cache(maxsize=1)
def embedder():
    from langchain_ollama import OllamaEmbeddings

    return OllamaEmbeddings(model=CFG.embed_model, base_url=CFG.ollama_url)


def _writer(demo: bool = False, max_tokens: int = 4096):
    """Private: only write() may build a generation client, so no call can skip the meter."""
    from langchain_openrouter import ChatOpenRouter

    model = CFG.gen_model_demo if demo else CFG.gen_model_dev
    kwargs = {}
    if model.startswith("anthropic/"):  # pin Anthropic as provider so prompt caching works
        kwargs["openrouter_provider"] = {"order": ["anthropic"], "allow_fallbacks": False}
    return model, ChatOpenRouter(model=model, api_key=CFG.openrouter_key, max_tokens=max_tokens, **kwargs)


def write(state: dict, step: str, messages, demo: bool | None = None, max_tokens: int = 4096):
    """One generation call. Returns (message, state_update)."""
    turns = meter.take_turn(state["run_id"], step)
    model, llm = _writer(state.get("demo", False) if demo is None else demo, max_tokens)
    call = meter.reserve_call(state["run_id"], step, model, worst_case_usd(model, messages, max_tokens), max_tokens)
    t0 = time.monotonic()
    try:
        msg = llm.invoke(messages)
    except Exception as e:
        ms = round((time.monotonic() - t0) * 1000)
        s = meter.settle_call(call, None, None, ms, error=type(e).__name__)
        events.log("model_call", key=call["_id"], model=model, ok=False, error=f"{type(e).__name__}: {str(e)[:200]}", ms=ms,
                   call_id=call["_id"], charged_usd=s["actual_usd"], note="cost unknown: charged the full reservation")
        raise
    ms = round((time.monotonic() - t0) * 1000)
    usage = msg.usage_metadata or {}
    s = meter.settle_call(call, usage, cost_usd(model, usage), ms)
    events.log("model_call", key=call["_id"], model=model, ok=True, ms=ms, call_id=call["_id"],
               input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
               reserved_usd=call["reserved_micro"] / meter.MICRO, cost_usd=s["actual_usd"])
    return msg, {"turns": turns, "spent_usd": s["spent_usd"]}
