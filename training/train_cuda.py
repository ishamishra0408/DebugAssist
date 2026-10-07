"""Run Laya's official training script on an NVIDIA GPU (e.g. Colab's T4).

The vendored script was written for Apple Silicon: its device picker only knows "mps" and "cpu". Instead of
editing the vendored file, we swap in a picker that returns the CUDA GPU, then run its main() unchanged.

Run:  python train_cuda.py --model-dir artifacts/laya_base --items artifacts/train_items.pt \
                           --output-dir artifacts/laya_triage_ft --epochs 2
"""
import sys

import torch

import vendor.laya_finetune_mps as ft

if not torch.cuda.is_available():
    sys.exit("No CUDA GPU found. In Colab: Runtime → Change runtime type → T4 GPU.")
ft.choose_device = lambda requested: torch.device("cuda")
sys.argv = ["laya_finetune_mps.py", *sys.argv[1:]]
ft.main()
