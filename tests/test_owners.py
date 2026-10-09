"""Runs are private to the person who started them, and each person has a monthly spending limit (Isha 2026-10-09:
"my runs should not be visible to him, and his not to me"; "each account should only be visible in their own login")."""
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from debug_assist import ghauth, meter, owners, server, viewer
from test_viewer import _data


@pytest.fixture
def two(scratch_db, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_CLIENT_ID", "Iv1.testclient")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "test-secret-not-real")
    monkeypatch.setenv("ALLOWED_GITHUB_USERS", "isha-gh, devansh-gh")
    monkeypatch.setattr(owners, "_coll", lambda: scratch_db["run_owners"])
    for rid in ("ai-1-20261001-100000", "ai-2-20261009-100000", "ai-3-20261009-110000"):
        (tmp_path / rid).mkdir()
    monkeypatch.setattr(server, "CFG", SimpleNamespace(runs_dir=tmp_path))
    monkeypatch.setattr(viewer, "gather", lambda rid, at=None: _data(run_id=rid))
    monkeypatch.setattr("debug_assist.store.reachable", lambda ttl_s=15.0: True)
    monkeypatch.setattr(server, "_status_of", lambda rid: (rid, "Done."))
    owners.record("ai-3-20261009-110000", "devansh-gh")        # Devansh's; the other two are from before owners
    return scratch_db


def _as(httpd, who, path):
    jar = f"da_session={ghauth.make_session(who)}" if who else ""
    req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}{path}", headers={"Cookie": jar})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_each_person_sees_only_their_own_runs_even_by_a_direct_link(two):
    httpd = server.make(0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        isha, dev = _as(httpd, "isha-gh", "/")[1], _as(httpd, "devansh-gh", "/")[1]
        # older runs are the first listed account's, written down the first time she lists them
        assert "ai-1-20261001-100000" in isha and "ai-2-20261009-100000" in isha and "ai-3-20261009-110000" not in isha
        assert "ai-3-20261009-110000" in dev and "ai-1-20261001-100000" not in dev and "ai-2-20261009-100000" not in dev
        assert two["run_owners"].count_documents({"owner": "isha-gh"}) == 2
        assert _as(httpd, "isha-gh", "/run/ai-3-20261009-110000")[0] == 404        # someone else's: as if it did not exist
        assert _as(httpd, "devansh-gh", "/replay/ai-1-20261001-100000")[0] == 404
        assert _as(httpd, "devansh-gh", "/run/ai-3-20261009-110000")[0] == 200
        code, _ = _as(httpd, "devansh-gh", "/run/latest")
        assert code == 200                                                            # his latest, not hers
    finally:
        httpd.shutdown()
    assert not server.owns("isha-gh", "ai-3-20261009-110000") and server.owns("devansh-gh", "ai-3-20261009-110000")


def test_a_run_starts_only_within_the_persons_monthly_limit(two, monkeypatch):
    monkeypatch.setenv("SPEND_CAPS", "devansh-gh=3")
    assert owners.cap_for("devansh-gh") == 3 and owners.cap_for("isha-gh") == owners.DEFAULT_CAP_USD
    meter.open_run("ai-3-20261009-110000", 2.5, 1800)
    meter._db()["meters"].update_one({"_id": "ai-3-20261009-110000"}, {"$set": {"spent_micro": meter.micro(1.2)}})
    meter.open_run("ai-1-20261001-100000", 2.5, 1800)                            # hers: never counted against him
    meter._db()["meters"].update_one({"_id": "ai-1-20261001-100000"}, {"$set": {"spent_micro": meter.micro(4)}})
    assert round(owners.spent_this_month("devansh-gh", server._runs()), 2) == 1.2
    with pytest.raises(server.Refused, match=r"up to \$2\.50, and \$1\.80 is left of your \$3\.00 this month\. Choose Standard AI"):
        server._within_limit("devansh-gh", "opus")
    server._within_limit("devansh-gh", "standard")                                # $0.50 fits in what is left
    server._within_limit("", "opus")                                              # no sign-in (this Mac): no limit
    monkeypatch.setenv("SPEND_CAP_USD", "abc")
    assert owners.cap_for("isha-gh") == owners.DEFAULT_CAP_USD                    # a bad setting falls back, never 0
