"""Automatic start: only a correctly signed GitHub delivery is read; only the label on an issue in a connected repo
queues it; one automatic run per issue; one run at a time; a daily cap; off unless switched on."""
import hashlib
import hmac
import json
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from debug_assist import autostart, server


class Coll:
    """Enough of a MongoDB collection for the queue."""

    def __init__(self):
        self.docs = {}

    def find_one(self, f):
        return self.docs.get(f["_id"])

    def insert_one(self, d):
        self.docs[d["_id"]] = dict(d)

    def update_one(self, f, u):
        self.docs[f["_id"]].update(u["$set"])

    def count_documents(self, f):
        return sum(1 for d in self.docs.values() if d["status"] == f["status"] and d.get("started_at", "") >= f["started_at"]["$gte"])

    def find(self, f):
        rows = [d for d in self.docs.values() if d["status"] == f["status"]]
        return SimpleNamespace(sort=lambda k, o: SimpleNamespace(limit=lambda n: sorted(rows, key=lambda d: d[k])[:n]))


def labelled(repo="acme/py", number=7, label="debug-assist", pr=False):
    return {"action": "labeled", "label": {"name": label}, "repository": {"full_name": repo},
            "issue": {"number": number, "title": "merge drops args", **({"pull_request": {}} if pr else {})}}


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("AUTO_RUNS", "1")
    monkeypatch.setattr("debug_assist.profiles.ready", lambda: ["vercel/ai", "acme/py"])
    return Coll()


def test_signatures():
    body = b'{"x":1}'
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert autostart.signature_ok(body, good, "s3cret")
    assert not autostart.signature_ok(body + b" ", good, "s3cret") and not autostart.signature_ok(body, good, "other")
    assert not autostart.signature_ok(body, None, "s3cret") and not autostart.signature_ok(body, good, "")  # no secret: never


def test_only_the_label_on_an_issue_in_a_connected_repo_queues_it(on):
    assert autostart.on_event("ping", {}, on) == (200, "pong")
    assert autostart.on_event("issues", {**labelled(), "action": "opened"}, on)[1].startswith("ignored")
    assert "not debug-assist" in autostart.on_event("issues", labelled(label="bug"), on)[1]
    assert "pull request" in autostart.on_event("issues", labelled(pr=True), on)[1]
    assert autostart.on_event("issues", labelled(repo="someone/else"), on) == (202, "not connected")
    assert autostart.on_event("issues", labelled(), on) == (202, "queued")
    assert autostart.on_event("issues", labelled(), on) == (202, "already queued or run")   # once per issue
    assert on.docs["acme/py#7"]["status"] == "queued" and on.docs["acme/py#7"]["source"] == "label"


def test_off_unless_switched_on(on, monkeypatch):
    monkeypatch.delenv("AUTO_RUNS")
    assert "automatic runs are off" in autostart.on_event("issues", labelled(), on)[1] and not on.docs


def test_the_worker_starts_one_at_a_time_and_says_why_when_it_cannot(on, monkeypatch):
    autostart.enqueue("acme/py", 7, "a", "label", on)
    autostart.enqueue("acme/py", 8, "b", "scan", on)
    started = []
    assert autostart.tick(lambda link: started.append(link) or "py-7-x", lambda: True, on) is None     # a run is going
    got = autostart.tick(lambda link: started.append(link) or "py-7-x", lambda: False, on)
    assert got["status"] == "started" and started == ["https://github.com/acme/py/issues/7"] and got["run_id"] == "py-7-x"

    def refuse(link):
        raise server.Refused("acme/py is not connected yet.")
    assert autostart.tick(refuse, lambda: False, on)["status"] == "refused" and "not connected" in on.docs["acme/py#8"]["why"]
    autostart.enqueue("acme/py", 9, "c", "scan", on)
    monkeypatch.setattr(autostart, "MAX_PER_DAY", 1)
    assert autostart.tick(lambda link: "x", lambda: False, on) is None and "already today" in on.docs["acme/py#9"]["why"]


def test_the_scan_catches_labels_the_webhook_missed(on, monkeypatch):
    monkeypatch.setattr("debug_assist.github_read.api", lambda path: [{"number": 3, "title": "t"}, {"number": 4, "pull_request": {}}]
                        if path.startswith("repos/acme/py/issues?labels=debug-assist") else [])
    assert autostart.scan(on) == 1 and list(on.docs) == ["acme/py#3"]


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CFG", SimpleNamespace(runs_dir=tmp_path))
    httpd = server.make(0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()


def _post(httpd, body: bytes, headers: dict):
    req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/hooks/github", data=body, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_the_webhook_reads_only_signed_deliveries(live, monkeypatch):
    body = json.dumps(labelled()).encode()
    assert _post(live, body, {"X-GitHub-Event": "issues"})[0] == 503            # no secret set: not set up
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s3cret")
    seen = []
    monkeypatch.setattr(autostart, "on_event", lambda ev, payload: (seen.append((ev, payload)), (202, "queued"))[1])
    assert _post(live, body, {"X-GitHub-Event": "issues", "X-Hub-Signature-256": "sha256=00"})[0] == 401 and not seen
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert _post(live, body, {"X-GitHub-Event": "issues", "X-Hub-Signature-256": sig}) == (202, {"result": "queued"})
    assert seen[0][0] == "issues" and seen[0][1]["issue"]["number"] == 7
