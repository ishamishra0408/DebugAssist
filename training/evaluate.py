"""Evaluate a Laya checkpoint (MLX on a Mac, or PyTorch on a GPU such as Colab).

TARGET   triage on our held-out split: is_defect (yes/no) and kind (bug / feature / docs-or-question).
HARD     8 scout-verified bugs + 9 look-alike non-bugs (so "say yes to everything" can't pass).
COUNTER  general decision skill on Laya's own benchmark (LocalLLaMA/typed-decisions test), so fine-tuning can't
         quietly break the other decisions the pipeline relies on.
Saves every single prediction, so two runs can be compared item by item (see compare.py).

Run:  python evaluate.py --model <dir or hub id> --backend torch --split test --general 80 --out baseline.json
"""
import argparse
import json
import time
import warnings
from collections import defaultdict
from pathlib import Path

import numpy as np

warnings.filterwarnings("ignore")
DATA = Path(__file__).parent / "data"


def load_agent(model: str, backend: str):
    if backend == "mlx":
        import laya_mlx
        return laya_mlx.load(model)
    import laya
    import torch
    return laya.Agent(model, device="cuda" if torch.cuda.is_available() else "cpu")


def ece(conf, correct, bins=10):
    """Expected calibration error: when the model says 80% sure, is it right ~80% of the time? 0 = perfect."""
    conf, correct = np.asarray(conf, float), np.asarray(correct, float)
    edges = np.linspace(0, 1, bins + 1)
    total = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (conf > lo) & (conf <= hi)
        if m.any():
            total += m.mean() * abs(conf[m].mean() - correct[m].mean())
    return round(float(total), 4)


def eval_triage(agent, rows):
    preds = []
    for r in rows:
        state, qs, gold = json.loads(r["state"]), json.loads(r["questions"]), json.loads(r["gold"])
        a = agent.predict(state, qs)["answers"]
        p_true = a["is_defect"]["noul"]
        gold_def = gold["is_defect"]["probabilities"]["true"] == 1.0
        gold_kind = max(gold["kind"]["probabilities"], key=gold["kind"]["probabilities"].get)
        preds.append({"id": r["id"], "repo": r["repo"], "label": r["label"], "p_defect": round(p_true, 4),
                      "def_ok": bool((p_true >= 0.5) == gold_def), "def_conf": max(p_true, 1 - p_true),
                      "kind": a["kind"]["choice"], "kind_ok": a["kind"]["choice"] == gold_kind,
                      "kind_conf": a["kind"]["answer_confidence"]})
    n = len(preds)

    def block(ok_key, conf_key):
        ok = [p[ok_key] for p in preds]
        return {"correct": int(sum(ok)), "n": n, "acc": round(float(np.mean(ok)), 4) if n else None,
                "ece": ece([p[conf_key] for p in preds], ok) if n else None}

    by_repo = defaultdict(lambda: [0, 0])
    for p in preds:
        by_repo[p["repo"]][0] += p["def_ok"]
        by_repo[p["repo"]][1] += 1
    return {"n": n, "is_defect": block("def_ok", "def_conf"), "kind": block("kind_ok", "kind_conf"),
            "is_defect_by_repo": {k: f"{v[0]} of {v[1]}" for k, v in by_repo.items()}, "predictions": preds}


def eval_general(agent, n_cases, seed=7):
    from datasets import load_dataset
    ds = load_dataset("LocalLLaMA/typed-decisions", "all", split="test").shuffle(seed=seed).select(range(n_cases))
    recs = []
    for row in ds:
        state, qs, gold = json.loads(row["state"]), json.loads(row["questions"]), json.loads(row["gold"])
        a = agent.predict(state, qs)["answers"]
        for qid, q in qs.items():
            if qid not in gold or qid not in a or q["type"] not in ("choice", "noul"):
                continue
            g = str(gold[qid].get("label")).lower()
            if q["type"] == "choice":
                ok, conf = str(a[qid]["choice"]).lower() == g, a[qid]["answer_confidence"]
            else:
                p = a[qid]["noul"]
                ok, conf = ("true" if p >= 0.5 else "false") == g, max(p, 1 - p)
            recs.append({"id": f"{row['id']}:{qid}", "ok": bool(ok), "conf": conf})
    ok = [r["ok"] for r in recs]
    return {"cases": n_cases, "decisions": len(ok), "correct": int(sum(ok)), "acc": round(float(np.mean(ok)), 4),
            "ece": ece([r["conf"] for r in recs], ok), "predictions": recs}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--backend", choices=["mlx", "torch"], default="torch")
    ap.add_argument("--split", default="test")
    ap.add_argument("--general", type=int, default=80, help="typed-decisions test cases for the counter check")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    agent = load_agent(args.model, args.backend)
    t0 = time.perf_counter()
    rows = [json.loads(l) for l in open(DATA / f"{args.split}.jsonl")]
    hard = [json.loads(l) for l in open(DATA / "hard.jsonl")]
    report = {"model": args.model, "backend": args.backend, "split": args.split,
              "triage": eval_triage(agent, rows), "hard": eval_triage(agent, hard)}
    if args.general:
        report["general_counter"] = eval_general(agent, args.general)
    report["seconds"] = round(time.perf_counter() - t0, 1)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2))

    t, h = report["triage"], report["hard"]
    hb = [p for p in h["predictions"] if p["label"] == "bug"]
    hn = [p for p in h["predictions"] if p["label"] != "bug"]
    print(f"model {args.model}  ({report['seconds']}s, {args.backend})")
    print(f"  is_defect  {t['is_defect']['correct']} of {t['n']}  acc {t['is_defect']['acc']}  ece {t['is_defect']['ece']}")
    print(f"  kind       {t['kind']['correct']} of {t['n']}  acc {t['kind']['acc']}  ece {t['kind']['ece']}")
    print(f"  by repo    {t['is_defect_by_repo']}")
    print(f"  HARD       bugs found {sum(p['def_ok'] for p in hb)} of {len(hb)} | "
          f"look-alike non-bugs rejected {sum(p['def_ok'] for p in hn)} of {len(hn)}")
    if "general_counter" in report:
        g = report["general_counter"]
        print(f"  COUNTER    general decisions {g['correct']} of {g['decisions']}  acc {g['acc']}  ece {g['ece']}")


if __name__ == "__main__":
    main()
