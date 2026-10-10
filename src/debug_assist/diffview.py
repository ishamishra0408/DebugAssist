"""Git-style diffs (Isha 2026-10-08: "like VS Code / git, the diff highlighted: removed red, added green").

  pr_patch(checkout)   the whole change a pull request would carry: the fix (git diff against the pinned commit) plus
                       every new file the run left in the code (its tests), each as a "new file" diff
  parse(patch)         files → hunks → lines, with old and new line numbers
  html(patch)          one card per file: path, +added −removed, hunks; removed lines red, added green, @@ headers blue

The run's own scratch never reaches a pull request: the lasting guard (kept in the run folder) and anything git
ignores are left out.
"""
import re
import subprocess
from pathlib import Path

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def _git(checkout: Path, *args) -> str:
    return subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True, timeout=120).stdout


def changed_paths(checkout: Path) -> list[str]:
    """Tracked files the copy changed (the fix, and any test file cases were added to)."""
    return [p for p in _git(Path(checkout), "diff", "--name-only").split() if p]


def new_file_diff(path: str, text: str) -> str:
    lines = text.splitlines()
    return "\n".join([f"diff --git a/{path} b/{path}", "new file mode 100644", "--- /dev/null", f"+++ b/{path}",
                      f"@@ -0,0 +1,{len(lines)} @@", *(f"+{l}" for l in lines)]) + "\n"


def pr_patch(checkout: Path) -> str:
    """The fix plus the run's new files (its tests), as one unified diff. Guards (da-guard-*) stay out."""
    checkout = Path(checkout)
    out = _git(checkout, "diff", "--no-color", "--no-ext-diff")
    for rel in sorted(_git(checkout, "ls-files", "--others", "--exclude-standard").split()):
        if "da-guard-" in rel or rel.startswith("."):
            continue
        f = checkout / rel
        if f.is_file() and f.stat().st_size < 200_000:
            out += new_file_diff(rel, f.read_text(errors="replace"))
    return out


def parse(patch: str) -> list[dict]:
    """[{path, new, deleted, added, removed, hunks: [{header, rows: [(kind, old_no, new_no, text)]}]}]"""
    files, cur, hunk, o, n = [], None, None, 0, 0
    for line in (patch or "").splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            cur = {"path": m.group(2) if m else line[11:], "new": False, "deleted": False, "added": 0, "removed": 0, "hunks": []}
            files.append(cur)
            hunk = None
        elif cur is None:
            continue
        elif line.startswith("new file mode"):
            cur["new"] = True
        elif line.startswith("deleted file mode"):
            cur["deleted"] = True
        elif line.startswith(("index ", "--- ", "+++ ", "similarity", "rename ")):
            continue
        elif (m := HUNK.match(line)):
            o, n = int(m.group(1)), int(m.group(3))
            hunk = {"header": line, "rows": []}
            cur["hunks"].append(hunk)
        elif hunk is not None:
            if line.startswith("+"):
                hunk["rows"].append(("add", "", n, line[1:]))
                n += 1
                cur["added"] += 1
            elif line.startswith("-"):
                hunk["rows"].append(("del", o, "", line[1:]))
                o += 1
                cur["removed"] += 1
            elif line.startswith("\\"):
                hunk["rows"].append(("note", "", "", line))
            else:
                hunk["rows"].append(("ctx", o, n, line[1:] if line.startswith(" ") else line))
                o += 1
                n += 1
    return files


def _e(t) -> str:
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def html(patch: str, open_files: int = 6) -> str:
    """The diff as GitHub shows it: a card per file, folded after the first few."""
    files = parse(patch)
    if not files:
        return '<p class="foot">No changes.</p>'
    cards = []
    for i, f in enumerate(files):
        tag = " · new file" if f["new"] else " · deleted" if f["deleted"] else ""
        rows = []
        for h in f["hunks"]:
            rows.append(f'<tr class="dh"><td class="ln"></td><td class="ln"></td><td class="code">{_e(h["header"])}</td></tr>')
            for kind, o, n, text in h["rows"]:
                sign = {"add": "+", "del": "-", "ctx": " ", "note": ""}[kind]
                rows.append(f'<tr class="d{kind}"><td class="ln">{o}</td><td class="ln">{n}</td>'
                            f'<td class="code"><span class="sg">{sign}</span>{_e(text)}</td></tr>')
        cards.append(f'<details class="dfile"{" open" if i < open_files else ""}><summary><span class="dpath">{_e(f["path"])}</span>'
                     f'<span class="dtag">{tag}</span><span class="dstat"><span class="plus">+{f["added"]}</span>'
                     f'<span class="minus">−{f["removed"]}</span></span></summary>'
                     f'<div class="dscroll"><table class="dtable">{"".join(rows)}</table></div></details>')
    add, rem = sum(f["added"] for f in files), sum(f["removed"] for f in files)
    return (f'<div class="diffview"><p class="dsum">{len(files)} file{"s" * (len(files) != 1)} changed · '
            f'<span class="plus">+{add}</span> <span class="minus">−{rem}</span></p>{"".join(cards)}</div>')


def is_test(path: str) -> bool:
    from .lang import is_test_path
    return is_test_path(path)


def kind_of_test(path: str) -> str:
    """unit | integration | automation, from the run's own naming (da-repro-<issue>-<rung>-<n>) or the path."""
    name = Path(path).name.lower()
    if "integration" in name or "__fixtures__" in path:
        return "integration"
    if "end_to_end" in name or "e2e" in path.lower():
        return "automation"
    return "unit"
