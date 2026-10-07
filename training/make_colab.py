"""Build the Colab teaching notebook and the upload bundle.

Run (on the Mac): uv run python make_colab.py
Produces: colab/laya_triage_finetune.ipynb  and  colab/laya_colab_bundle.zip
"""
import json
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "colab"


def md(text):
    return {"cell_type": "markdown", "metadata": {}, "source": text.strip("\n").splitlines(keepends=True)}


def code(text):
    return {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
            "source": text.strip("\n").splitlines(keepends=True)}


CELLS = [
    md("""
# Fine-tuning Laya to triage GitHub issues

**Goal:** teach Laya (a small, open-weight *decision* model) to answer one question better:
**"Is this a real defect in this repo's own code?"**. That's the first step of our Debug Assist pipeline.

**What you'll learn, step by step**
1. Measure a **baseline** *before* changing anything
2. Spot **label leakage** (the trap we already hit once)
3. Why we **replay** old training data (so the model doesn't forget its other skills)
4. What **calibration** means and why we measure it
5. How to decide "did it really improve?" with a **paired comparison**, not a gut feeling

**Before you start:** `Runtime → Change runtime type → T4 GPU → Save`. Everything below runs on Google's GPU, not your Mac.

Each step has three parts: **What this does**, **What to look for**, and **The principle**.
"""),
    md("""
## Step 0 · Check the GPU
**What this does:** confirms Colab gave you an NVIDIA GPU.
**What to look for:** a line mentioning `Tesla T4` and `cuda available: True`.
"""),
    code("""
!nvidia-smi --query-gpu=name,memory.total --format=csv
import torch
print("cuda available:", torch.cuda.is_available(), "| torch", torch.__version__)
"""),
    md("""
## Step 1 · Upload the bundle
**What this does:** a file picker appears. Choose `laya_colab_bundle.zip` (from `DebugAssist/training/colab/` on your Mac).
It holds our dataset and 5 small scripts. **No secrets are in it.**
"""),
    code("""
from google.colab import files
uploaded = files.upload()
!mkdir -p /content/work && unzip -q -o laya_colab_bundle.zip -d /content/work
%cd /content/work
!ls -R | head -30
"""),
    md("""
## Step 2 · Install Laya
**What this does:** installs Laya's PyTorch runtime. Colab already has PyTorch.
"""),
    code("""
!pip install -q "laya>=0.3" "transformers>=4.48" datasets safetensors huggingface_hub
import laya; print("laya", laya.__version__ if hasattr(laya, "__version__") else "installed")
"""),
    md("""
## Step 3 · Look at the data first
**What this does:** prints how many examples each split has, and one example the model will see.

**What to look for:** the classes are roughly balanced in every split. The example text has **no template
checkboxes** like `- [x] This is a bug, not a usage question`.

**The principle: label leakage.** langchain's issue template contains a ticked checkbox that *states the answer*.
Our first version of this dataset kept it in 713 of 1,549 rows. A model trained on that would learn to read the
checkbox, not the issue, and score brilliantly while learning nothing. An independent reviewer caught it before
any training ran. **Always look at what the model actually sees.**
"""),
    code("""
import json, collections, textwrap
for split in ["train", "val", "test", "hard"]:
    rows = [json.loads(l) for l in open(f"data/{split}.jsonl")]
    print(f"{split:5s} {len(rows):5d}", dict(collections.Counter(r["label"] for r in rows)))
ex = json.loads(open("data/train.jsonl").readline())
print("\\nExample id:", ex["id"], "| label:", ex["label"])
print(textwrap.shorten(json.loads(ex["state"]), 600))
print("\\nQuestions asked:", list(json.loads(ex["questions"]).keys()))
"""),
    md("""
## Step 4 · Download the starting model
**What this does:** downloads Laya's English checkpoint (~0.85 GB). We only take the English model, not the whole
2.4 GB repo.
"""),
    code("""
from huggingface_hub import snapshot_download
from laya.agent import _fix_tokenizer_config
snapshot_download("convaiinnovations/laya", local_dir="artifacts/laya_base",
                  allow_patterns=["config.json", "model.safetensors", "rl_agent_config.json", "tokenizer/*"])
_fix_tokenizer_config("artifacts/laya_base")
!du -sh artifacts/laya_base
"""),
    md("""
## Step 5 · Baseline: measure before you change anything
**What this does:** scores the *untouched* model on three things:
- **Test set** (232 issues it will never train on): is it a defect? which kind?
- **Hard set**: 8 bugs our scouts verified by hand **+ 9 look-alike non-bugs** (text that sounds like a bug but isn't).
  A model that says "bug" to everything can't pass this.
- **Counter-metric**: 80 cases from Laya's own benchmark. Our pipeline also uses Laya for other decisions (is this
  guard real or just advice? does this story name a person?), so fine-tuning must not damage those.

**What to look for:** the `is_defect … of 232` and `COUNTER … of …` lines. **Write them down; they're the bar.**
`ece` is the calibration error: when the model says "80% sure", is it right about 80% of the time? 0 is perfect.

**The principle:** without a baseline, "it got better" is a feeling. (~5–10 min)
"""),
    code("""
!python evaluate.py --model artifacts/laya_base --backend torch --split test --general 80 --out artifacts/baseline.json
"""),
    md("""
## Step 6 · Build the training items
**What this does:** turns our 1,061 training issues (2 questions each) into training items, then mixes in
**300 cases from Laya's original training data**.

**The principle: replay against "catastrophic forgetting".** A model fine-tuned only on a new task can lose skills it
had. Mixing in a slice of the old training data keeps those skills alive, and Step 5's counter-metric checks it worked.
"""),
    code("""
!python prepare_items.py --replay-cases 300
"""),
    md("""
## Step 7 · Train
**What this does:** runs Laya's official training script on the T4. Two passes over ~3,300 items; at the end it holds
out ~370 items to **calibrate** the confidence scores. It prints a loss line every 100 steps. **Expect roughly 15–40
minutes.** Keep this tab open: free Colab disconnects idle sessions.

**What to look for:** `Device: cuda`, steady progress lines, and finally `Running temperature calibration …`.

**The principle:** training changes the model's weights to make the right answers more likely. Calibration then adjusts
*how confident* it says it is, without changing *what* it answers. They are separate jobs, so they're measured separately.
"""),
    code("""
!python train_cuda.py --model-dir artifacts/laya_base --items artifacts/train_items.pt --output-dir artifacts/laya_triage_ft --epochs 2 --micro-batch 2 --grad-accum 16
"""),
    md("""
## Step 8 · Evaluate the fine-tuned model on exactly the same items
**What this does:** the same three checks as Step 5, on the new model.
"""),
    code("""
!python evaluate.py --model artifacts/laya_triage_ft --backend torch --split test --general 80 --out artifacts/finetuned.json
"""),
    md("""
## Step 9 · Did it really improve? Paired comparison
**What this does:** compares the two runs **item by item**. For each question it counts
**gained** (wrong before, right after) and **lost** (right before, wrong after).

**The principle:** "181 → 183" could be luck. Because both models answered the *same* items, we look only at the
items that **flipped**, and call it real only if `gained − lost > 2 × √(gained + lost)`.

**Adoption rule (decided before training, so the bar can't move afterwards):**
1. a real gain on "is it a defect?"
2. the hard set doesn't get worse (bugs found, look-alikes rejected)
3. the counter-metric shows no real loss
4. calibration error stays ≤ 0.05

All four must pass, or we keep the original model. **That's a legitimate outcome, not a failure.**
"""),
    code("""
!python compare.py artifacts/baseline.json artifacts/finetuned.json
"""),
    md("""
## Step 10 · Bring the results back
**What this does:** zips the fine-tuned model (~0.85 GB) and both reports, then downloads them.
Put the zip in `DebugAssist/training/artifacts/` on your Mac and tell Claude. It'll convert the model for the fast
local path (a few seconds) and, **only if the verdict was ADOPT**, wire it into the pipeline.
"""),
    code("""
!cd artifacts && zip -q -r laya_triage_results.zip laya_triage_ft baseline.json finetuned.json && du -sh laya_triage_results.zip
files.download("artifacts/laya_triage_results.zip")
"""),
    md("""
## What you learned
| Principle | Where you saw it |
|---|---|
| Measure a baseline first | Step 5 |
| Look at the data: label leakage hides in templates | Step 3 |
| Replay old data to prevent forgetting | Step 6, checked by the counter in Steps 5 and 8 |
| Accuracy and calibration are different things | Step 7 (training vs calibration), the `ece` numbers |
| Decide the bar *before* the result; judge with paired flips | Step 9 |
"""),
]


def main():
    OUT.mkdir(exist_ok=True)
    nb = {"cells": CELLS, "metadata": {"accelerator": "GPU", "colab": {"provenance": [], "gpuType": "T4"},
                                        "kernelspec": {"display_name": "Python 3", "name": "python3"},
                                        "language_info": {"name": "python"}},
          "nbformat": 4, "nbformat_minor": 0}
    (OUT / "laya_triage_finetune.ipynb").write_text(json.dumps(nb, indent=1))
    files = ["evaluate.py", "compare.py", "prepare_items.py", "train_cuda.py", "vendor/__init__.py",
             "vendor/laya_finetune_mps.py", "data/train.jsonl", "data/val.jsonl", "data/test.jsonl", "data/hard.jsonl"]
    # Gate: refuse to build an upload that contains anything shaped like a credential.
    from build_dataset import SECRET_VALUE
    leaks = [f for f in files if SECRET_VALUE.search((HERE / f).read_text(errors="ignore"))]
    if leaks:
        raise SystemExit(f"REFUSED: key-like values found in {leaks}; rebuild the dataset (it redacts them)")
    with zipfile.ZipFile(OUT / "laya_colab_bundle.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(HERE / f, f)
    print("notebook:", OUT / "laya_triage_finetune.ipynb")
    print("bundle:  ", OUT / "laya_colab_bundle.zip", f"({(OUT / 'laya_colab_bundle.zip').stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
