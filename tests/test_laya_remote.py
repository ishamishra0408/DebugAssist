"""Laya on the Mac for hosted runs: answers only with the shared secret, and hosted runs get the same answers."""
import dataclasses
import json
import threading
import urllib.error
import urllib.request

import pytest

from debug_assist import laya_server, models

TOKEN = "t" * 32


class FakeLaya:
    def predict(self, text, questions):
        return {"answers": {q: {"choice": "yes", "noul": 0.9, "answer_confidence": 0.9} for q in questions}}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(models, "laya", lambda which="general": FakeLaya())
    httpd = laya_server.make(0, TOKEN)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _post(url, token, body):
    req = urllib.request.Request(url + "/decide", method="POST", data=json.dumps(body).encode(),
                                 headers={"X-Laya-Token": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, {}


def test_the_mac_answers_only_with_the_secret(api):
    q = {"is_defect": {"type": "choice", "instructions": "x", "criteria": {"yes": "y", "no": "n"}}}
    assert _post(api, "wrong", {"text": "t", "questions": q})[0] == 403
    assert _post(api, "", {"text": "t", "questions": q})[0] == 403
    code, out = _post(api, TOKEN, {"text": "t", "questions": q, "which": "triage"})
    assert code == 200 and out["answers"]["is_defect"]["choice"] == "yes"
    assert _post(api, TOKEN, {"text": "t", "questions": q, "which": "../etc"})[0] == 400


def test_a_hosted_run_asks_the_mac_and_gets_the_same_shape(api, monkeypatch):
    monkeypatch.setattr(models, "CFG", dataclasses.replace(models.CFG, laya_url=api, laya_token=TOKEN))
    got = models.decide("text", {"a1": {"type": "choice", "instructions": "x", "criteria": {"yes": "y", "no": "n"}}})
    assert got["a1"]["choice"] == "yes" and got["a1"]["noul"] == 0.9


def test_it_refuses_to_start_without_a_long_secret(monkeypatch):
    monkeypatch.setenv("LAYA_TOKEN", "short")
    with pytest.raises(SystemExit, match="LAYA_TOKEN"):
        laya_server.serve(0)
