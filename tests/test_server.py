"""The localhost viewer: GET only, 127.0.0.1 only, run ids checked, replay clock shortened and monotonic."""
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from debug_assist import server, viewer
from test_viewer import _data


@pytest.fixture
def live(tmp_path, monkeypatch):
    (tmp_path / "ai-1-x").mkdir()
    monkeypatch.setattr(server, "CFG", SimpleNamespace(runs_dir=tmp_path))
    monkeypatch.setattr(viewer, "gather", lambda rid, at=None: _data(run_id=rid))
    httpd = server.make(0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()


def _get(httpd, path, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}{path}", method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, ""


def test_the_viewer_listens_on_localhost_only_and_only_reads(live):
    assert live.server_address[0] == "127.0.0.1"
    assert _get(live, "/health") == (200, "debug-assist viewer")
    code, page = _get(live, "/run/ai-1-x")
    assert code == 200 and 'data-k="node-0"' in page and "fetch(u" in page and 'http-equiv="refresh"' not in page
    assert "method:" not in page and "<form" not in page  # the page only GETs its own address
    for method in ("POST", "PUT", "DELETE"):
        assert _get(live, "/run/ai-1-x", method)[0] == 501


def test_run_ids_are_checked_before_anything_is_read(live):
    for bad in ("/run/..%2F..%2Fetc", "/run/../x", "/run/nope", "/run/.hidden", "/other/ai-1-x"):
        assert _get(live, bad)[0] == 404


def test_the_replay_clock_shortens_long_waits_and_never_runs_backwards():
    t0 = datetime(2026, 10, 7, 6, 25, tzinfo=timezone.utc)
    times = [t0, t0 + timedelta(seconds=8), t0 + timedelta(minutes=30), t0 + timedelta(minutes=30, seconds=4)]
    plan = viewer.plan_from(times, speed=8, max_gap_s=4)
    assert [o for o, _ in plan] == [0, 1.0, 5.0, 5.5]                  # 8 s → 1 s; 30 min → 4 s; 4 s → 0.5 s
    seen = [viewer.real_at(plan, x / 10) for x in range(0, 70)]
    assert seen == sorted(seen) and seen[0] == t0 and seen[-1] == times[-1]
    assert viewer.real_at(plan, 3.0) - times[1] == (times[2] - times[1]) / 2   # time runs evenly inside a gap


def test_replay_picks_the_checkpoint_the_run_was_at():
    t0 = datetime(2026, 10, 7, 6, 25, tzinfo=timezone.utc)
    hist = [SimpleNamespace(created_at=(t0 + timedelta(seconds=s)).isoformat(), n=s) for s in (60, 30, 0)]  # newest first
    assert viewer.pick(hist, t0 + timedelta(seconds=45)).n == 30
    assert viewer.pick(hist, t0 - timedelta(seconds=1)) is None
