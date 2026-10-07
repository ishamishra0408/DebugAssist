"""Compare two evaluation runs item by item, and decide whether the fine-tune is adopted.

Why paired: both models answered the SAME questions. Count the items that flipped:
  b = wrong before, right after   (gains)
  c = right before, wrong after   (losses)
A real improvement needs b − c > 2·√(b + c) (a sign-test rule of thumb, ~95%). One extra right answer is noise.

Adoption rule (DECISIONS.md, 2026-10-06), all must hold:
  1. is_defect on test: a real gain, by the paired rule above
  2. hard set: the fine-tune finds at least as many of the 8 bugs AND rejects at least as many look-alikes
  3. counter: general decision skill shows no real loss, by the same paired rule
  4. is_defect calibration error stays ≤ 0.05

Run:  python compare.py baseline.json finetuned.json
"""
import json
import math
import sys


def flips(before: list, after: list, key: str):
    a = {p["id"]: p[key] for p in after}
    b = sum(1 for p in before if not p[key] and a.get(p["id"]) is True)
    c = sum(1 for p in before if p[key] and a.get(p["id"]) is False)
    real = (b - c) > 2 * math.sqrt(b + c) if (b + c) else False
    real_loss = (c - b) > 2 * math.sqrt(b + c) if (b + c) else False
    return b, c, real, real_loss


def main(base_path, ft_path):
    B, F = json.load(open(base_path)), json.load(open(ft_path))
    rows = []

    b, c, gain, _ = flips(B["triage"]["predictions"], F["triage"]["predictions"], "def_ok")
    rows.append(("1 is_defect gain (test)",
                 f"{B['triage']['is_defect']['correct']} → {F['triage']['is_defect']['correct']} of "
                 f"{F['triage']['n']}  (gained {b}, lost {c})", gain))

    kb, kc, kgain, kloss = flips(B["triage"]["predictions"], F["triage"]["predictions"], "kind_ok")
    rows.append(("  kind (reported, not gating)",
                 f"{B['triage']['kind']['correct']} → {F['triage']['kind']['correct']}  (gained {kb}, lost {kc})",
                 None))

    def hard_counts(r):
        hb = [p for p in r["hard"]["predictions"] if p["label"] == "bug"]
        hn = [p for p in r["hard"]["predictions"] if p["label"] != "bug"]
        return sum(p["def_ok"] for p in hb), len(hb), sum(p["def_ok"] for p in hn), len(hn)

    b1, nb, n1, nn = hard_counts(B)
    b2, _, n2, _ = hard_counts(F)
    rows.append(("2 hard set holds",
                 f"bugs {b1} → {b2} of {nb}; look-alikes rejected {n1} → {n2} of {nn}", b2 >= b1 and n2 >= n1))

    if "general_counter" in B and "general_counter" in F:
        gb, gc, _, gloss = flips(B["general_counter"]["predictions"], F["general_counter"]["predictions"], "ok")
        rows.append(("3 counter: no real loss",
                     f"{B['general_counter']['correct']} → {F['general_counter']['correct']} of "
                     f"{F['general_counter']['decisions']}  (gained {gb}, lost {gc})", not gloss))

    e = F["triage"]["is_defect"]["ece"]
    rows.append(("4 calibration ≤ 0.05", f"is_defect ECE {B['triage']['is_defect']['ece']} → {e}", e <= 0.05))

    for name, detail, ok in rows:
        mark = "    " if ok is None else ("PASS" if ok else "FAIL")
        print(f"{mark}  {name:30s} {detail}")
    gating = [ok for _, _, ok in rows if ok is not None]
    print("\nVERDICT:", "ADOPT the fine-tune" if all(gating) else "KEEP zero-shot Laya (fine-tune did not clear every rule)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
