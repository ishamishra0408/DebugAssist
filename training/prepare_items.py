"""Turn our triage rows (+ a replay slice of Laya's own benchmark) into training items for the vendored script.

Replay: training only on triage could erode the other decisions the pipeline asks Laya (guard class, checks).
Mixing in a slice of the original typed-decisions train set protects that; the evaluate.py counter checks it.

Run: uv run python prepare_items.py --replay-cases 300
"""
import argparse
import json
import random
from pathlib import Path

import torch
from huggingface_hub import snapshot_download
from transformers import AutoTokenizer

from laya.agent import _fix_tokenizer_config
from vendor.laya_finetune_mps import build_training_item

HERE = Path(__file__).parent
BASE = HERE / "artifacts" / "laya_base"


def items_from(rows, tokenizer, cfg):
    out, skipped = [], 0
    for row in rows:
        state, qs, gold = json.loads(row["state"]), json.loads(row["questions"]), json.loads(row["gold"])
        for qid, q in qs.items():
            if qid not in gold:
                continue
            it = build_training_item(tokenizer, cfg, state, q, gold[qid])
            if it is None:
                skipped += 1
            else:
                out.append(it)
    return out, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay-cases", type=int, default=300)
    ap.add_argument("--out", default=str(HERE / "artifacts" / "train_items.pt"))
    args = ap.parse_args()

    snapshot_download("convaiinnovations/laya", local_dir=str(BASE),
                      allow_patterns=["config.json", "model.safetensors", "rl_agent_config.json", "tokenizer/*"])
    _fix_tokenizer_config(str(BASE))
    tokenizer = AutoTokenizer.from_pretrained(BASE / "tokenizer")
    cfg = json.load(open(BASE / "rl_agent_config.json"))
    cfg = {**cfg, "max_len": cfg.get("max_len", 1024), "head_max_len": cfg.get("head_max_len", 256)}

    ours = [json.loads(l) for l in open(HERE / "data" / "train.jsonl")]
    triage_items, s1 = items_from(ours, tokenizer, cfg)

    replay_items, s2 = [], 0
    if args.replay_cases:
        from datasets import load_dataset
        ds = load_dataset("LocalLLaMA/typed-decisions", "all", split="train").shuffle(seed=7)
        replay_items, s2 = items_from(ds.select(range(min(args.replay_cases, len(ds)))), tokenizer, cfg)

    items = triage_items + replay_items
    random.Random(7).shuffle(items)
    torch.save(items, args.out)  # no sidecar meta → the vendored script uses this file as-is
    print(f"triage items {len(triage_items)} (skipped {s1}) + replay items {len(replay_items)} (skipped {s2}) "
          f"= {len(items)} → {args.out}")


if __name__ == "__main__":
    main()
