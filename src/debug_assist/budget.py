"""Spend guardrail, part 1: prices, turn caps per step, the dollar cap per run, and each call's worst case.
Part 2, the crash-proof running total, is meter.py (MongoDB). The OpenRouter key cap is the outer limit."""
from .config import CFG

# USD per million tokens: (input, output, cached input). OpenRouter live price list, 2026-10-06.
PRICES = {
    "anthropic/claude-opus-5.5": (4.00, 20.00, 0.20),
    "anthropic/claude-sonnet-5.5": (2.00, 10.00, 0.20),
    "qwen/qwen3-coder-next": (0.12, 0.80, 0.12),
}


class BudgetExceeded(RuntimeError):
    pass


class TurnCapExceeded(RuntimeError):
    pass


def cost_usd(model: str, usage: dict) -> float:
    """Price one call from LangChain usage_metadata. Unknown model → refuse (fail closed, never run unpriced)."""
    if model not in PRICES:
        raise BudgetExceeded(f"no price on file for {model}; add it to PRICES before using it")
    p_in, p_out, p_cache = PRICES[model]
    cached = (usage.get("input_token_details") or {}).get("cache_read", 0) or 0
    fresh = max(usage.get("input_tokens", 0) - cached, 0)
    return (fresh * p_in + cached * p_cache + usage.get("output_tokens", 0) * p_out) / 1_000_000


def run_cap(state: dict) -> float:
    """Demo runs (Opus) get the demo cap; everything else the dev cap."""
    return CFG.demo_budget_usd if state.get("demo") else CFG.run_budget_usd


def worst_case_usd(model: str, messages, max_tokens: int) -> float:
    """The most one call can cost, computed BEFORE it is made. Input is bounded by UTF-8 bytes (a byte-level
    tokenizer never makes more tokens than bytes; our reading for tokenizers that aren't published) plus a margin
    per message; output is bounded by max_tokens, which the provider enforces."""
    if model not in PRICES:
        raise BudgetExceeded(f"no price on file for {model}; add it to PRICES before using it")
    p_in, p_out, _ = PRICES[model]
    n_bytes = sum(len(str(m[1] if isinstance(m, tuple) else getattr(m, "content", m)).encode()) for m in messages)
    return ((n_bytes + 16 * len(messages)) * p_in + max_tokens * p_out) / 1_000_000
