"""Build the advisor seats page from REAL sources, never hand-typed status (same rule as the progress board).

  what each seat is for   ← its own SKILL.md (frontmatter description, else its title line)
  every review it gave    ← the bundle's usage_log.jsonl receipts (append-only; row 0 is the bundle's example)
  the gates it signs      ← the bundle's GATES.tsv
  gate results            ← DECISIONS.md ("<Stage> stage: DONE"), and for build-eval the eval spec on disk

Run:  uv run python scripts/seats.py      → ~/Downloads/debug-assist-pipeline/seats/index.html
"""
import csv
import html
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HOME = Path.home()
BUNDLE = HOME / "Downloads" / "isha-advisor-bundle-2026-10-05 2"
WORK = HOME / "Downloads" / "debug-assist-pipeline"
OUT = WORK / "seats"
SEATS = ["allspaw", "metric-design", "harness-design", "qe-ic-advisor", "ds-ic", "de-advisor"]
WHERE = {  # where each seat's SKILL.md is read from, and a one-word state for the page
    "de-advisor": (HOME / ".claude/skills/de-advisor/SKILL.md", "v2 installed 2026-10-07"),
    "qe-ic-advisor": (HOME / ".claude/skills/qe-ic-advisor/SKILL.md", "installed"),
}
THIN = {"ds-ic": "flagged thin by its owner; upgrade or a signer ruling due Friday"}
e = lambda x: html.escape(str(x if x is not None else ""))


def seat_purpose(seat: str) -> tuple[str, str]:
    path, state = WHERE.get(seat, (BUNDLE / seat / "SKILL.md", "bundle"))
    if not path.exists():
        return "(no SKILL.md found)", state
    text = path.read_text(errors="ignore")
    m = re.search(r"^description:\s*(.+)$", text, re.M)
    if m:
        first = re.split(r"(?<=[.!?])\s", m.group(1).strip())[0]
        return first, state
    h1 = re.search(r"^#\s+[^\n]*?[—-]\s+(.+)$", text, re.M)
    return (h1.group(1).strip() if h1 else "(no description)"), state


def receipts() -> tuple[dict, dict]:
    rows = [json.loads(l) for l in (BUNDLE / "usage_log.jsonl").read_text().splitlines() if l.strip()]
    example, real = rows[0], rows[1:]
    by = defaultdict(list)
    for r in real:
        by[r["seat"]].append(r)
    return by, example


def _read(p: Path) -> str:
    return p.read_text() if p.exists() else ""


def gates() -> list[dict]:
    with open(BUNDLE / "GATES.tsv") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    dec = (WORK / "DECISIONS.md").read_text().lower()
    for g in rows:
        stage = g["stage"]
        label = {"build-eval": "build-eval"}.get(stage, stage)
        if f"{label} stage: done" in dec or f"{stage.title()} stage: DONE".lower() in dec:
            g["state"] = "PASS"
        elif stage == "build-eval" and "conditions closed" in _read(WORK / "design" / "eval-spec-v1.md").lower():
            g["state"] = "READY TO RULE"   # the pre-checks' conditions are closed; the stage is Isha's call
        elif stage == "build-eval" and (WORK / "design" / "eval-spec-v0.md").exists():
            g["state"] = "PREP"
        else:
            g["state"] = "not started"
        g["seats_list"] = [s.strip() for s in g["seat"].split(",")]
    return rows


def page() -> str:
    by, example = receipts()
    gs = gates()
    signs = defaultdict(list)
    for g in gs:
        for s in g["seats_list"]:
            signs[s].append(g)
    total = sum(len(v) for v in by.values())
    gate_cards = "".join(
        f'<li class="gate {g["state"].replace(" ", "-").lower()}"><span class="pill">{e(g["state"])}</span>'
        f'<b>{e(g["stage"])}</b><span class="gname">{e(g["gate_name"])}</span>'
        f'<p>{e(g["criterion"])}</p><p class="cond"><span>pass</span> {e(g["pass_condition"])}</p>'
        f'<p class="cond"><span>fail</span> {e(g["fail_condition"])}</p>'
        f'<p class="signers">{" · ".join(e(s) for s in g["seats_list"])}</p></li>' for g in gs)
    seat_rows = []
    for s in SEATS:
        purpose, where = seat_purpose(s)
        rs = sorted(by.get(s, []), key=lambda r: r["ts"])
        last = rs[-1] if rs else None
        flag = f'<span class="flag">{e(THIN[s])}</span>' if s in THIN else ""
        gate_tags = "".join(f'<span class="tag {g["state"].replace(" ", "-").lower()}">{e(g["stage"])}</span>' for g in signs.get(s, []))
        items = "".join(
            f'<li><time>{e(r["ts"][:16].replace("T", " "))}</time><p class="q">{e(r["question"])}</p>'
            f'<p><span class="lbl">said</span> {e(r["output_summary"])}</p>'
            f'<p><span class="lbl">changed</span> {e(r["decision_changed"])}</p></li>' for r in reversed(rs))
        seat_rows.append(
            f'<details class="seat"{" open" if s in ("qe-ic-advisor", "ds-ic", "de-advisor") else ""}><summary>'
            f'<span class="name">{e(s)}</span><span class="count">{len(rs)}</span>'
            f'<span class="last">{e(last["output_summary"][:150]) + "…" if last and len(last["output_summary"]) > 150 else e(last["output_summary"]) if last else "no review yet"}</span>'
            f'<span class="gates">{gate_tags}</span></summary>'
            f'<div class="body"><p class="purpose">{e(purpose)}</p><p class="where">{e(where)} {flag}</p>'
            f'<ol class="reviews">{items or "<li><p>No receipt yet: a seat that was not asked has not reviewed anything.</p></li>"}</ol></div></details>')
    gen = datetime.now().strftime("%a %d %b %Y, %H:%M")
    return f"""<title>Advisor Seats</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
/* Layout: the four gates as a row of tickets (the pipeline's stages in order), then one expandable row per seat,
   newest review first. Same palette and faces as the progress board, so the two pages read as one set. */
:root {{ --bg:#f6f7f9; --surface:#ffffff; --fg:#1f242c; --muted:#5d6675; --line:#dfe3e9; --accent:#2b5fae;
  --ok:#2f7a4f; --okbg:#e3f2e8; --warn:#a36400; --warnbg:#fdf0d5; --idle:#8b93a1; --idlebg:#eef0f3;
  --sans:"IBM Plex Sans",system-ui,sans-serif; --mono:"IBM Plex Mono",ui-monospace,monospace }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg:#0f1216; --surface:#171b21; --fg:#e7eaef;
  --muted:#9aa3b2; --line:#2a3039; --accent:#7ea6ea; --ok:#5fbf86; --okbg:#1a2e22; --warn:#e2a33b; --warnbg:#3a2c12;
  --idle:#6b7382; --idlebg:#262a31; color-scheme:dark }} }}
:root[data-theme="dark"] {{ --bg:#0f1216; --surface:#171b21; --fg:#e7eaef; --muted:#9aa3b2; --line:#2a3039; --accent:#7ea6ea;
  --ok:#5fbf86; --okbg:#1a2e22; --warn:#e2a33b; --warnbg:#3a2c12; --idle:#6b7382; --idlebg:#262a31; color-scheme:dark }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.55 var(--sans); padding-inline:16px; padding-block:28px 48px }}
main {{ max-width:1100px; margin:0 auto; display:grid; gap:24px }}
h1 {{ font-size:26px; font-weight:600; margin:0; letter-spacing:-.01em; text-wrap:balance }}
h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:0 0 10px; font-weight:500 }}
.sub {{ color:var(--muted); margin:4px 0 0; font-size:14px }}
ol.gates {{ list-style:none; margin:0; padding:0; display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:12px }}
.gate {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:12px 14px; min-width:0; display:grid; gap:4px; align-content:start }}
.gate b {{ font-size:16px }} .gname {{ font:13px var(--mono); color:var(--muted) }}
.gate p {{ margin:0; font-size:13.5px }} .cond {{ color:var(--muted) }} .cond span {{ font:12px var(--mono); text-transform:uppercase; margin-right:4px }}
.signers {{ font:12.5px var(--mono); color:var(--accent); margin-top:4px !important }}
.pill,.tag {{ justify-self:start; font:500 12px var(--mono); padding:2px 8px; border-radius:999px; background:var(--idlebg); color:var(--idle) }}
.pass .pill,.tag.pass {{ background:var(--okbg); color:var(--ok) }} .prep .pill,.tag.prep,.ready-to-rule .pill,.tag.ready-to-rule {{ background:var(--warnbg); color:var(--warn) }}
.seat {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; min-width:0 }}
.seat + .seat {{ margin-top:10px }}
summary {{ list-style:none; cursor:pointer; display:grid; grid-template-columns:150px 36px 1fr auto; gap:12px; align-items:center; padding:12px 14px }}
summary::-webkit-details-marker {{ display:none }} summary:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; border-radius:10px }}
.name {{ font-weight:600 }} .count {{ font:500 18px var(--mono); font-variant-numeric:tabular-nums; text-align:right }}
.last {{ color:var(--muted); font-size:13.5px; min-width:0 }} .gates {{ display:flex; gap:6px; flex-wrap:wrap; justify-content:flex-end }}
.body {{ border-top:1px solid var(--line); padding:12px 14px 14px; display:grid; gap:8px }}
.purpose {{ margin:0 }} .where {{ margin:0; font-size:13px; color:var(--muted) }}
.flag {{ margin-left:8px; color:var(--warn) }}
ol.reviews {{ list-style:none; margin:0; padding:0; display:grid; gap:10px }}
.reviews li {{ border-left:3px solid var(--line); padding-left:12px; min-width:0 }} .reviews p {{ margin:2px 0; font-size:13.5px; overflow-wrap:anywhere }}
time {{ font:12px var(--mono); color:var(--muted) }} .q {{ font-weight:500 }}
.lbl {{ font:11.5px var(--mono); text-transform:uppercase; letter-spacing:.04em; color:var(--muted); margin-right:6px }}
footer {{ color:var(--muted); font-size:12.5px }} code {{ font-family:var(--mono) }}
@media (max-width:640px) {{ summary {{ grid-template-columns:1fr auto; }} .last,.gates {{ grid-column:1 / -1; justify-content:flex-start }} }}
</style>
<main>
  <header>
    <h1>Advisor Seats</h1>
    <p class="sub">Six seats · {total} reviews logged · generated {gen} from the bundle's receipts and gates</p>
  </header>
  <section><h2>The four gates, in stage order</h2><ol class="gates">{gate_cards}</ol></section>
  <section><h2>Seats · click one for every question it was asked</h2>{"".join(seat_rows)}</section>
  <footer>Every review here is a receipt in <code>usage_log.jsonl</code> (append-only; its first row is the bundle's
  example and is not counted: "{e(example.get("output_summary"))}"). Gate state comes from DECISIONS.md; build-eval reads
  PREP while its eval spec is drafted and READY TO RULE once the pre-checks' conditions are closed. Regenerate with <code>uv run python scripts/seats.py</code>.</footer>
</main>
"""


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "index.html").write_text(page())
    print(f"wrote {OUT / 'index.html'}")


if __name__ == "__main__":
    main()
