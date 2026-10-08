"""Preflight gate: check every dependency BEFORE a run starts, and refuse with every named reason at once.

harness-design review (2026-10-06): today 3 of 6 runs died mid-way (one at step 7, after the whole fix phase) on a
missing piece. A missing dependency is a refusal at the door, not an exception in the middle.

Each check returns PASS / FAIL / WARN with the fact it saw and the exact fix. The run starts only if nothing FAILs.
Ruled 2026-10-06: Phoenix down refuses the run, unless the run is started with --no-trace (recorded as trace OFF).
"""
import json
import subprocess
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import CFG
from .github_read import parse_issue_url
from .profiles import UnknownRepo, profile_for


@dataclass
class Check:
    name: str
    status: str  # PASS | FAIL | WARN
    fact: str
    fix: str = ""


def _http(url, headers=None, timeout=4):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=timeout) as r:
            return r.status, json.load(r), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, None, {}
    except Exception as e:  # unreachable is a fact, not an exception
        return None, str(e), {}


def check_e2b(template: str = "") -> Check:
    """Hosted runs: the E2B key and the repo's template exist, and a sandbox starts and ends (about a second, a
    fraction of a cent). Each connected repo has its own template; vercel/ai's is E2B_TEMPLATE."""
    import os
    template = template or CFG.e2b_template
    if not os.environ.get("E2B_API_KEY"):
        return Check("Sandbox (E2B)", "FAIL", "E2B_API_KEY not set", "add it to the host's environment yourself (never in chat)")
    if not template:
        return Check("Sandbox (E2B)", "FAIL", "no E2B template for this repo",
                     "connect the repo (Connect a repo page); vercel/ai: uv run python scripts/e2b_template.py vercel/ai --build")
    try:
        from e2b import Sandbox
        sbx = Sandbox.create(template=template, timeout=60, allow_internet_access=False)
        sbx.kill()
    except Exception as e:
        return Check("Sandbox (E2B)", "FAIL", f"could not start a sandbox ({type(e).__name__}: {str(e)[:120]})",
                     "check the key and the template name (connecting the repo again rebuilds it)")
    return Check("Sandbox (E2B)", "PASS", f"template {template} starts with internet access off")


def check_docker(image: str | None) -> Check:
    try:
        up = subprocess.run(["docker", "info"], capture_output=True, timeout=8).returncode == 0
    except Exception:
        up = False
    if not up:
        return Check("Docker", "FAIL", "daemon not reachable", "Docker Desktop: whale menu ▸ Resume (or start it)")
    if image:
        has = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=15).returncode == 0
        if not has:
            return Check("Docker", "FAIL", f"sandbox image not present: {image[:40]}…", f"docker pull {image}")
    return Check("Docker", "PASS", "daemon up" + (", sandbox image present" if image else ""))


def check_mongo() -> Check:
    try:
        from pymongo import MongoClient
        c = MongoClient(CFG.mongodb_uri, serverSelectionTimeoutMS=3000)
        hello = c.admin.command("hello")
    except Exception as e:
        return Check("MongoDB", "FAIL", f"not reachable ({type(e).__name__})", "docker compose up -d mongodb")
    if not hello.get("isWritablePrimary"):
        return Check("MongoDB", "FAIL", "not a writable primary (replica set stranded?)",
                     "keep the hostname pin and the mongo-config volume in docker-compose.yml; see README")
    return Check("MongoDB", "PASS", f"writable primary, replica set {hello.get('setName')}")


def check_vector_index(expected_dims: int | None) -> Check:
    try:
        from pymongo import MongoClient
        coll = MongoClient(CFG.mongodb_uri, serverSelectionTimeoutMS=3000)[CFG.db_name]["conditions"]
        n = coll.estimated_document_count()
        idx = list(coll.list_search_indexes("conditions_vec"))
    except Exception as e:
        return Check("Vector index", "FAIL", f"could not read ({type(e).__name__})", "fix MongoDB first")
    if n == 0:
        return Check("Vector index", "PASS", "corpus empty; index is created with the first stored condition")
    if not idx:
        return Check("Vector index", "WARN", f"{n} conditions but no index yet", "it is created on the next run")
    if not idx[0].get("queryable"):
        return Check("Vector index", "FAIL", "index exists but is not queryable yet (rebuilding after restart?)",
                     "wait ~30–90 s and rerun preflight")
    dims = idx[0].get("latestDefinition", {}).get("fields", [{}])[0].get("numDimensions")
    if expected_dims and dims and dims != expected_dims:
        return Check("Vector index", "FAIL", f"index has {dims} dims but the embedding model gives {expected_dims}",
                     "embedding model changed; rebuild the index or switch back")
    return Check("Vector index", "PASS", f"queryable, {n} conditions, {dims} dims")


def check_voyage() -> tuple[Check, int | None]:
    import os
    if not os.environ.get("VOYAGE_API_KEY"):
        return Check("Embeddings", "FAIL", "VOYAGE_API_KEY not set", "add it to the host's environment yourself"), None
    try:
        from .models import embedder
        dims = len(embedder().embed_query("preflight"))
    except Exception as e:
        return Check("Embeddings", "FAIL", f"Voyage did not answer ({type(e).__name__})", "check the key"), None
    return Check("Embeddings", "PASS", f"Voyage {CFG.embed_model} answers, {dims} dims"), dims


def check_ollama() -> tuple[Check, int | None]:
    code, tags, _ = _http(f"{CFG.ollama_url}/api/tags")
    if code != 200:
        return Check("Ollama + Qwen3", "FAIL", "Ollama not running", "open -a Ollama"), None
    names = [m["name"] for m in tags.get("models", [])]
    if not any(n.startswith(CFG.embed_model.split(":")[0]) and n.endswith(CFG.embed_model.split(":")[-1])
               for n in names):
        return Check("Ollama + Qwen3", "FAIL", f"model {CFG.embed_model} not pulled",
                     f"ollama pull {CFG.embed_model}"), None
    try:
        from langchain_ollama import OllamaEmbeddings
        dims = len(OllamaEmbeddings(model=CFG.embed_model, base_url=CFG.ollama_url).embed_query("preflight"))
    except Exception as e:
        return Check("Ollama + Qwen3", "FAIL", f"embed call failed ({type(e).__name__})", "restart Ollama"), None
    return Check("Ollama + Qwen3", "PASS", f"{CFG.embed_model} answers, {dims} dims"), dims


def check_laya() -> Check:
    if CFG.laya_url:  # hosted: Laya answers from the Mac through the tunnel
        return check_remote_laya()
    triage = Path(CFG.laya_triage_checkpoint) / "model.safetensors"
    if not triage.exists():
        return Check("Laya", "FAIL", f"fine-tuned triage model missing at {triage.parent}",
                     "re-convert from the Colab zip (README: training)")
    # laya-mlx downloads only the files it needs, so check exactly those (a whole-repo check falsely refuses)
    from huggingface_hub import try_to_load_from_cache
    needed = ["encoder/config.json", "mlx_config.json", "model.safetensors", "rl_agent_config.json",
              "tokenizer/tokenizer.json"]  # exact paths, read from the HF cache on 2026-10-06
    missing = [f for f in needed if not isinstance(try_to_load_from_cache(CFG.laya_checkpoint, f), str)]
    if missing:
        return Check("Laya", "FAIL", f"general model {CFG.laya_checkpoint} missing cached files: {missing}",
                     "run once online: uv run python -c \"import laya_mlx; laya_mlx.load('aac6fef/laya-mlx')\"")
    return Check("Laya", "PASS", "triage (fine-tuned) + general checkpoints on disk")


def check_remote_laya() -> Check:
    """The Mac's Laya API is up, reachable, and accepts our token (one tiny decision, milliseconds, $0)."""
    if len(CFG.laya_token) < 24:
        return Check("Laya", "FAIL", "LAYA_URL is set but LAYA_TOKEN is missing or short", "set the same LAYA_TOKEN here and on the Mac")
    try:
        from .models import _remote_decide
        _remote_decide("probe", {"ok": {"type": "choice", "instructions": "Is this a probe?",
                                        "criteria": {"yes": "it is", "no": "it is not"}}}, "general")
    except Exception as e:
        return Check("Laya", "FAIL", f"the Mac's Laya API did not answer ({type(e).__name__}: {str(e)[:100]})",
                     "on the Mac: LAYA_TOKEN=... uv run debug-assist laya-serve, and keep the tunnel running")
    return Check("Laya", "PASS", "answering from the Mac through the tunnel")


def check_openrouter(demo: bool) -> Check:
    if not CFG.openrouter_key:
        return Check("OpenRouter", "FAIL", "OPENROUTER_API_KEY not set", "add it to .env yourself (never in chat)")
    code, body, _ = _http("https://openrouter.ai/api/v1/key", {"Authorization": f"Bearer {CFG.openrouter_key}"}, 8)
    if code != 200:
        return Check("OpenRouter", "FAIL", f"key rejected or unreachable (HTTP {code})", "check the key / network")
    d = body["data"]
    need = CFG.demo_budget_usd if demo else CFG.run_budget_usd
    if d.get("limit") is None:
        return Check("OpenRouter", "FAIL", "key has no spending cap", "set a cap on the key in the OpenRouter dashboard")
    if (d.get("limit_remaining") or 0) < need:
        return Check("OpenRouter", "FAIL", f"only ${d.get('limit_remaining'):.2f} left; this run may need ${need:.2f}",
                     "top up or raise the key cap")
    if not demo and d["limit_remaining"] < CFG.demo_budget_usd:
        # enough for a Standard run, not for a Claude Opus run (2026-10-08: System check said "Working", then the
        # Opus run of #22288 was refused at the start)
        return Check("OpenRouter", "WARN", f"key OK, ${d['limit_remaining']:.2f} left: enough for Standard runs "
                                           f"(${CFG.run_budget_usd:.2f}), not for a Claude Opus run (${CFG.demo_budget_usd:.2f})",
                     "raise the key's cap in OpenRouter (Settings, API Keys) for Claude Opus runs")
    return Check("OpenRouter", "PASS", f"key OK, ${d['limit_remaining']:.2f} of ${d['limit']:.0f} cap left "
                                       f"(run cap ${need:.2f})")


def check_github(owner: str, repo: str) -> Check:
    tok = CFG.github_token
    if not tok:
        return Check("GitHub token", "FAIL", "GITHUB_TOKEN_READONLY not set", "add a fine-grained read-only token to .env")
    if not tok.startswith("github_pat_"):
        return Check("GitHub token", "FAIL", "not a fine-grained token (classic tokens carry broad scopes)",
                     "create a fine-grained token: Public repositories (read-only)")
    code, _, hdr = _http(f"https://api.github.com/repos/{owner}/{repo}",
                         {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}, 8)
    if code != 200:
        return Check("GitHub token", "FAIL", f"cannot read {owner}/{repo} (HTTP {code}); expired?", "renew the token")
    left = int(hdr.get("X-RateLimit-Remaining") or hdr.get("x-ratelimit-remaining") or 0)
    if left < 100:
        return Check("GitHub token", "FAIL", f"only {left} API calls left this hour", "wait for the reset")
    return Check("GitHub token", "PASS", f"fine-grained, read OK, {left} calls left")


def phoenix_traces_url(endpoint: str) -> str:
    """Where traces go: the endpoint with /v1/traces added, as phoenix.otel does."""
    base = endpoint.rstrip("/")
    return base if base.endswith("/v1/traces") else base + "/v1/traces"


def check_phoenix_cloud(trace: bool) -> Check:
    """Hosted runs trace to Phoenix Cloud. Sends one empty trace with the key: tests the address and the key together
    (fixed 2026-10-08: the first version asked the address for JSON and read its web page as 'not reachable')."""
    import os
    import urllib.error
    key = os.environ.get("PHOENIX_API_KEY", "")
    if not key:
        return (Check("Phoenix", "WARN", "no key, but run started with --no-trace (recorded as trace OFF)") if not trace else
                Check("Phoenix", "FAIL", "PHOENIX_API_KEY not set: this run would be untraced", "add it, or pass --no-trace"))
    url = phoenix_traces_url(CFG.phoenix_endpoint)
    req = urllib.request.Request(url, data=b"", method="POST", headers={
        "Content-Type": "application/x-protobuf", "authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:
        return Check("Phoenix", "FAIL" if trace else "WARN", f"{url} not reachable ({type(e).__name__})", "check the address")
    if code in (401, 403):
        return Check("Phoenix", "FAIL" if trace else "WARN", "the address answers but refuses the key",
                     "make a new System Key in Phoenix Cloud ▸ Settings and set PHOENIX_API_KEY")
    if code == 404:
        return Check("Phoenix", "FAIL" if trace else "WARN", f"{url} is not a traces address",
                     "copy the Hostname from Phoenix Cloud ▸ Settings into PHOENIX_COLLECTOR_ENDPOINT")
    if code >= 400:
        return Check("Phoenix", "FAIL" if trace else "WARN", f"Phoenix Cloud answered HTTP {code}", "try again in a minute")
    return Check("Phoenix", "PASS", "Phoenix Cloud accepts traces with this key")


def check_phoenix(trace: bool, boot_wait_s: int = 20) -> Check:
    """Phoenix takes ~10 s to boot after `docker compose up -d`. If its container is running but not answering yet,
    wait up to boot_wait_s before deciding, rather than refusing a service that is visibly starting."""
    import time
    code, _, _ = _http("http://127.0.0.1:6006/v1/projects", timeout=3)
    if code != 200:
        try:
            running = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", "debugassist-phoenix-1"],
                                     capture_output=True, text=True, timeout=5).stdout.strip() == "true"
        except Exception:
            running = False
        waited = 0
        while running and code != 200 and waited < boot_wait_s:
            time.sleep(2)
            waited += 2
            code, _, _ = _http("http://127.0.0.1:6006/v1/projects", timeout=3)
        if code == 200:
            return Check("Phoenix", "PASS", f"tracing endpoint up (waited {waited} s for it to finish booting)")
    if code == 200:
        return Check("Phoenix", "PASS", "tracing endpoint up")
    if not trace:
        return Check("Phoenix", "WARN", "down, but run started with --no-trace (recorded as trace OFF)")
    return Check("Phoenix", "FAIL", "down: this run would be untraced",
                 "cd ~/Projects/DebugAssist && docker compose up -d phoenix   (or pass --no-trace)")


def check_base(profile) -> Check:
    from . import checkout
    ok, fact = checkout.check_base(profile)
    return Check("Base checkout", "PASS" if ok else "FAIL", fact,
                 "" if ok else "uv run python scripts/prepare_base.py " + profile.repo)


def check_advisors() -> Check:
    """Advice is optional, so off passes. An address without the review fails: nothing connects before it."""
    from .advisors import status
    st, why = status()
    if st == "OFF":
        return Check("Advisors", "PASS", "off: not connected (advice is optional)")
    if st == "BLOCKED":
        return Check("Advisors", "FAIL", why,
                     "review the Advisors MCP server first, then set ADVISORS_REVIEWED=yes in .env (or remove ADVISORS_MCP)")
    from .advisors import AdvisorError, mcp_call
    try:
        tools = [t.get("name") for t in mcp_call("tools/list", {}, timeout=60).get("tools") or []]
    except (AdvisorError, OSError) as e:
        return Check("Advisors", "WARN", f"on, but the server did not answer: {e}"[:200],
                     "advice is optional: the run goes on without it. Check ADVISORS_MCP and ADVISORS_KEY")
    if "advise" not in tools:
        return Check("Advisors", "WARN", f"the server answered but has no advise tool ({len(tools)} tools)",
                     "check ADVISORS_MCP points at the Domain Expertise MCP server")
    return Check("Advisors", "PASS", f"connected: the advise tool answers ({len(tools)} tools on the server)")


def run_preflight(issue_url: str, demo: bool = False, trace: bool = True) -> list[Check]:
    checks = []
    owner, repo, _ = parse_issue_url(issue_url)
    template = ""
    try:
        prof = profile_for(owner, repo)
        image, template = prof.image, prof.e2b_template
        checks.append(Check("Repo profile", "PASS", f"{owner}/{repo} has a sandbox profile"))
        checks.append(check_base(prof))
    except UnknownRepo as e:
        image = None
        checks.append(Check("Repo profile", "FAIL", str(e), "connect it first, from the Connect a repo page"))
    checks.append(check_e2b(template) if CFG.sandbox_backend == "e2b" else check_docker(image))
    mongo = check_mongo()
    checks.append(mongo)
    oll, dims = check_voyage() if CFG.embed_provider == "voyage" else check_ollama()
    checks.append(check_vector_index(dims) if mongo.status != "FAIL" else
                  Check("Vector index", "FAIL", "the database is not reachable", "fix MongoDB first"))
    checks.append(oll)
    checks.append(check_laya())
    checks.append(check_openrouter(demo))
    checks.append(check_github(owner, repo))
    checks.append(check_phoenix_cloud(trace) if not CFG.phoenix_endpoint.startswith(("http://127.", "http://localhost"))
                  else check_phoenix(trace))
    checks.append(check_advisors())
    return checks


def report(checks: list[Check], starting: bool = False) -> tuple[bool, str]:
    ok = not any(c.status == "FAIL" for c in checks)
    lines = [f"{'PREFLIGHT ' + c.status:16s} {c.name:15s} {c.fact}" + (f"\n{'':32s}fix: {c.fix}" if c.fix and
             c.status != "PASS" else "") for c in checks]
    verdict = ("PREFLIGHT PASS: starting the run" if starting else "PREFLIGHT PASS: ready to run (nothing was started)") if ok else \
        f"PREFLIGHT REFUSED: {sum(c.status == 'FAIL' for c in checks)} problem(s) above; nothing was started"
    return ok, "\n".join(lines + ["", verdict])


def as_records(checks: list[Check]) -> list[dict]:
    return [asdict(c) for c in checks]
