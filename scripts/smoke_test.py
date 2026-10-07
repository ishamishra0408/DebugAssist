"""Smoke test: is every component of the stack alive and talking? No paid model calls.

Run:  uv run python scripts/smoke_test.py
Each check prints PASS/FAIL with a measured number, so failures are facts, not guesses.
"""
import os
import time

from dotenv import load_dotenv

load_dotenv()
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27017/?directConnection=true")
PHOENIX = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://127.0.0.1:6006/v1/traces")
EMBED_MODEL = os.getenv("EMBED_MODEL", "qwen3-embedding:0.6b")
LAYA_CHECKPOINT = os.getenv("LAYA_CHECKPOINT", "aac6fef/laya-mlx")

results = []


def check(name):
    def wrap(fn):
        t0 = time.perf_counter()
        try:
            detail = fn()
            results.append((name, True, f"{detail} ({time.perf_counter() - t0:.2f}s)"))
        except Exception as e:  # report, never hide
            results.append((name, False, f"{type(e).__name__}: {e}"))
        return fn
    return wrap


@check("phoenix tracing")
def _phoenix():
    from phoenix.otel import register
    register(project_name="debug-assist-smoke", endpoint=PHOENIX, auto_instrument=True, batch=False)
    return f"tracer registered → {PHOENIX}"


@check("ollama embeddings (Qwen3)")
def _embed():
    from langchain_ollama import OllamaEmbeddings
    emb = OllamaEmbeddings(model=EMBED_MODEL)
    v = emb.embed_query("path inputs are not normalised before prefix matching")
    globals()["EMBED"] = emb
    return f"dim={len(v)}"


@check("mongodb vector search (Atlas Local)")
def _mongo():
    from pymongo import MongoClient
    from pymongo.operations import SearchIndexModel
    emb = globals()["EMBED"]
    client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    coll = client["debug_assist_smoke"]["conditions"]
    coll.drop()
    texts = [
        "path inputs are not normalised before prefix matching",
        "stream flush assumes the response finished cleanly",
        "security patch removed the json default serializer fallback",
    ]
    vecs = emb.embed_documents(texts)
    coll.insert_many([{"text": t, "embedding": v} for t, v in zip(texts, vecs)])
    coll.create_search_index(SearchIndexModel(
        name="vec", type="vectorSearch",
        definition={"fields": [{"type": "vector", "path": "embedding",
                                "numDimensions": len(vecs[0]), "similarity": "cosine"}]}))
    for _ in range(60):  # index build is async
        idx = list(coll.list_search_indexes("vec"))
        if idx and idx[0].get("queryable"):
            break
        time.sleep(1)
    q = emb.embed_query("trailing slash in a file path breaks the search")
    hits = list(coll.aggregate([{"$vectorSearch": {"index": "vec", "path": "embedding", "queryVector": q,
                                                     "numCandidates": 10, "limit": 1}},
                                {"$project": {"text": 1, "_id": 0}}]))
    assert hits, "vector search returned nothing"
    return f"top match: '{hits[0]['text']}'"


@check("laya decisions (MLX, local)")
def _laya():
    import laya_mlx as laya
    agent = laya.load(LAYA_CHECKPOINT)
    issue = ("grep_search returns 'No matches found' when path='/app/' but works with path='/app'. "
             "Repro: StateFileSearchMiddleware(...).grep_search(pattern='x', path='/app/'). "
             "Worked in 1.5.6, broken in 1.6.0.")
    t0 = time.perf_counter()
    r = agent.predict(issue, {
        "kind": {"type": "choice", "instructions": "What kind of GitHub issue is this?",
                 "criteria": ["bug", "usage question", "feature request"]},
        "has_repro": {"type": "noul", "instructions": "Does the issue include steps or code to reproduce it?"},
        "regression": {"type": "noul", "instructions": "Does it say it worked in an earlier version?"},
    })
    ms = (time.perf_counter() - t0) * 1000
    a = r["answers"]
    return f"kind={a['kind']} | has_repro={a['has_repro']} | regression={a['regression']} | warm call {ms:.0f}ms"


@check("langgraph + mongo checkpointer + pause/resume")
def _graph():
    from typing import TypedDict
    from langgraph.checkpoint.mongodb import MongoDBSaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt
    from pymongo import MongoClient

    class S(TypedDict):
        log: list

    def work(s):
        return {"log": s["log"] + ["work done"]}

    def approve(s):
        answer = interrupt({"ask": "go-word?"})
        return {"log": s["log"] + [f"approval={answer}"]}

    g = StateGraph(S)
    g.add_node("work", work)
    g.add_node("approve", approve)
    g.add_edge(START, "work")
    g.add_edge("work", "approve")
    g.add_edge("approve", END)
    saver = MongoDBSaver(MongoClient(MONGODB_URI), db_name="debug_assist_smoke")
    app = g.compile(checkpointer=saver)
    cfg = {"configurable": {"thread_id": f"smoke-{int(time.time())}"}}
    app.invoke({"log": []}, cfg)  # stops at interrupt
    paused = app.get_state(cfg).next
    out = app.invoke(Command(resume="go"), cfg)
    return f"paused at {paused}, resumed → {out['log']}"


@check("openrouter key present (no call made)")
def _openrouter():
    assert os.getenv("OPENROUTER_API_KEY"), "OPENROUTER_API_KEY not set in .env yet"
    return "set (value not printed)"


@check("github read-only token present")
def _gh():
    assert os.getenv("GITHUB_TOKEN_READONLY"), "GITHUB_TOKEN_READONLY not set in .env yet"
    return "set (value not printed)"


if __name__ == "__main__":
    width = max(len(n) for n, _, _ in results)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
    print(f"\n{sum(ok for _, ok, _ in results)} of {len(results)} pass")
