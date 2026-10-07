"""Settings, read once from .env. Secrets are held here and never logged."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Config:
    openrouter_key: str = os.getenv("OPENROUTER_API_KEY", "")
    github_token: str = os.getenv("GITHUB_TOKEN_READONLY", "")
    mongodb_uri: str = os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27017/?directConnection=true")
    phoenix_endpoint: str = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://127.0.0.1:6006/v1/traces")
    ollama_url: str = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    gen_model_dev: str = os.getenv("GEN_MODEL_DEV", "qwen/qwen3-coder-next")
    gen_model_demo: str = os.getenv("GEN_MODEL_DEMO", "anthropic/claude-opus-5.5")
    embed_model: str = os.getenv("EMBED_MODEL", "qwen3-embedding:0.6b")
    laya_checkpoint: str = os.getenv("LAYA_CHECKPOINT", "aac6fef/laya-mlx")  # general decisions (guard class, checks)
    # Fine-tuned on Colab 2026-10-06 for triage only (DECISIONS: option B). Test: is_defect 174 → 215 of 232.
    laya_triage_checkpoint: str = os.getenv("LAYA_TRIAGE_CHECKPOINT", str(ROOT / "models" / "laya-triage-mlx"))
    triage_review_below: float = 0.6  # confidence below this → a person checks the triage
    run_budget_usd: float = float(os.getenv("RUN_BUDGET_USD", "0.50"))     # dev runs (Qwen3-Coder-Next)
    demo_budget_usd: float = float(os.getenv("DEMO_BUDGET_USD", "2.50"))   # the Opus demo run (ruled 2026-10-06)
    # Total sandbox wall time per run (install + every test run), reserved before each command (meter.py)
    sandbox_budget_s: int = int(os.getenv("SANDBOX_BUDGET_S", "1800"))
    db_name: str = "debug_assist"
    runs_dir: Path = ROOT / "runs"


CFG = Config()

# Turn caps per step. Enforced in code (meter.take_turn, counted in MongoDB), not in a prompt.
# holdout 4: the second test (≤ 2 tries) and, after a round-2 fix, a fresh third test (≤ 2 tries; ruled 2026-10-07)
TURN_CAPS = {"reproduce": 4, "find_cause": 10, "write_fix": 8, "holdout": 4, "why_it_shipped": 2, "lasting_guard": 3}

# Reproduction ladder (ruled 2026-10-06): attempts across all rungs before the run stops NEVER REPRODUCED
REPRO_ATTEMPT_CAP = 4

# What ⏱ and 🎯 mean: north-star-v1.2, sealed by Isha 2026-10-07 (two judges, a fix that saw the second test counts as
# one unless a fresh third passes; changed packages + installed dependents stay green; 🎯 is k of m past siblings).
# v1.1 (234c6996…) is kept unchanged. A changed definition needs a new ruling, a new file and a new hash; a test checks it.
NORTH_STAR_DOC = Path.home() / "Downloads" / "debug-assist-pipeline" / "design" / "north-star-v1.2.md"
NORTH_STAR_SEAL = "ce88deda895444ef1de0d099fbcefa91dac63f9872bd490afe1b32217a25d917"
