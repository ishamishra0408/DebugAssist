"""The E2B template for hosted runs: the repo at the profile's pinned commit, installed and built, in /work. Same image,
same install and build commands as the local Docker sandbox, so a hosted run tests exactly the same code.

  uv run python scripts/e2b_template.py vercel/ai            # write deploy/e2b/<repo>/e2b.Dockerfile (no account needed)
  uv run python scripts/e2b_template.py vercel/ai --build    # also build it on E2B (needs E2B_API_KEY in your shell)

After --build, set E2B_TEMPLATE=<the alias it prints> where runs are hosted. The install runs once, here, with the
network on; every sandbox started from the template has internet access off.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from debug_assist.profiles import PROFILES  # noqa: E402


def dockerfile(prof) -> str:
    """The same recipe a run uses when it installs a missing package (pkgcheck.recipe), install list included."""
    from debug_assist.pkgcheck import recipe
    return recipe(prof)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1 or args[0] not in PROFILES:
        sys.exit(__doc__)
    from debug_assist import profiles
    prof = profiles.get(args[0])   # with the packages a person chose to install (pkgcheck.py)
    out = ROOT / "deploy" / "e2b" / prof.repo.replace("/", "-")
    out.mkdir(parents=True, exist_ok=True)
    (out / "e2b.Dockerfile").write_text(dockerfile(prof))
    alias = f"debugassist-{prof.repo.replace('/', '-').lower()}-{prof.base_commit[:7]}"
    print(f"wrote {out / 'e2b.Dockerfile'}")
    if "--build" in sys.argv:
        from e2b import Template
        info = Template.build(Template().from_dockerfile(str(out / "e2b.Dockerfile")), alias=alias, cpu_count=2,
                              memory_mb=4096, on_build_logs=lambda entry: print(getattr(entry, "message", entry)))
        print(f"\nbuilt: {info}\nset E2B_TEMPLATE={alias} where runs are hosted")
    else:
        print(f"to build it on E2B: E2B_API_KEY=... uv run python scripts/e2b_template.py {args[0]} --build  (alias {alias})")


if __name__ == "__main__":
    main()
