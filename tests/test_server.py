"""The localhost viewer: GET only, 127.0.0.1 only, run ids checked, replay clock shortened and monotonic."""
import json
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


def _get(httpd, path, method="GET", headers=None, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}{path}", method=method,
                                 headers=headers or {}, data=body)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_the_viewer_listens_on_localhost_only_and_only_reads(live):
    assert live.server_address[0] == "127.0.0.1"
    assert _get(live, "/health") == (200, "debug-assist viewer")
    code, page = _get(live, "/run/ai-1-x")
    assert code == 200 and 'data-k="step-0"' in page and "fetch(u" in page and 'http-equiv="refresh"' not in page
    assert "method:" not in page and "<form" not in page  # the page only GETs its own address
    for method in ("PUT", "DELETE"):
        assert _get(live, "/run/ai-1-x", method)[0] == 501
    assert _get(live, "/api/start", "POST")[0] == 403      # no token, no origin: refused


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


def _ok_headers(httpd):
    port = httpd.server_address[1]
    return {server.TOKEN_HEADER: server.TOKEN, "Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"}


def test_only_the_home_page_can_start_a_run(live, monkeypatch):
    started = []
    monkeypatch.setattr(server, "start_run", lambda u, h, a: started.append((u, h, a)) or "ai-2-y")
    body = json.dumps({"url": "https://github.com/vercel/ai/issues/2", "heading": "", "ai": "standard"}).encode()
    good = _ok_headers(live)
    for drop in (server.TOKEN_HEADER, "Origin", "Content-Type"):
        h = {k: v for k, v in good.items() if k != drop}
        assert _get(live, "/api/start", "POST", h, body)[0] == 403, drop
    assert _get(live, "/api/start", "POST", {**good, "Origin": "https://evil.example"}, body)[0] == 403
    assert _get(live, "/api/start", "POST", {**good, "Host": "evil.example"}, body)[0] == 403   # DNS rebinding
    assert _get(live, "/", "GET", {"Host": "evil.example"})[0] == 403
    code, out = _get(live, "/api/start", "POST", good, body)
    assert code == 200 and json.loads(out)["page"] == "/run/ai-2-y" and len(started) == 1
    monkeypatch.setattr(server, "start_run", lambda u, h, a: (_ for _ in ()).throw(server.Refused("A run is already going (x).")))
    code, out = _get(live, "/api/start", "POST", good, body)
    assert code == 409 and "already going" in json.loads(out)["error"]


def test_a_bad_link_gets_a_plain_answer(live):
    code, out = _get(live, "/api/issue?url=https://example.com/x", "GET", {server.TOKEN_HEADER: server.TOKEN})
    assert code == 400 and json.loads(out)["error"].startswith("That is not a GitHub issue link.")
    assert _get(live, "/api/issue?url=x")[0] == 403        # no token


def test_reading_an_issue_lists_the_sections_it_could_fix(monkeypatch):
    from debug_assist import github_read
    monkeypatch.setattr(server, "_ready_repo", lambda o, r: None)
    monkeypatch.setattr(github_read, "get_issue", lambda u: {"title": "T", "state": "open", "body":
                        "intro\n## Repro\nsteps here\n## Secondary observation\nthe flush emits a partial call\n## Empty\n"})
    got = server.read_issue("https://github.com/vercel/ai/issues/21439")
    assert [x["heading"] for x in got["sections"]] == ["Repro", "Secondary observation"]   # empty sections left out
    assert got["sections"][1]["preview"] == "the flush emits a partial call"


def test_a_run_starts_like_the_terminal_starts_it_and_only_one_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CFG", SimpleNamespace(runs_dir=tmp_path))
    monkeypatch.setattr(server, "_ready_repo", lambda o, r: None)
    monkeypatch.setattr(server, "read_issue", lambda u: {"sections": [{"heading": "Secondary observation"}]})
    calls = []

    class Proc:
        def __init__(self, argv, **kw):
            calls.append(argv)

        def poll(self):
            return None  # still running
    monkeypatch.setattr(server.subprocess, "Popen", Proc)
    monkeypatch.setitem(server._child, "proc", None)
    rid = server.start_run("https://github.com/vercel/ai/issues/21439", "Secondary observation", "opus")
    argv = calls[0]
    assert argv[2:5] == ["debug_assist", "run", "https://github.com/vercel/ai/issues/21439"]
    assert f"--run-id={rid}" in argv and "--no-view" in argv and "--demo" in argv
    assert "--focus-heading=Secondary observation" in argv and (tmp_path / rid / "console.log").exists()
    with pytest.raises(server.Refused, match="already going"):
        server.start_run("https://github.com/vercel/ai/issues/21439", "", "standard")
    monkeypatch.setitem(server._child, "proc", None)
    with pytest.raises(server.Refused, match="not in the issue"):
        server.start_run("https://github.com/vercel/ai/issues/21439", "Made up", "standard")


def test_a_hosted_address_needs_the_password(live, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "correct horse battery")
    monkeypatch.setenv("PUBLIC_HOST", "debugassist.onrender.com")
    monkeypatch.setattr(server, "_status_of", lambda rid: ("vercel/ai #1", "Done."))
    host = {"Host": "debugassist.onrender.com"}
    req = urllib.request.Request(f"http://127.0.0.1:{live.server_address[1]}/", headers=host)
    opener = urllib.request.build_opener(type("NoRedirect", (urllib.request.HTTPRedirectHandler,), {"redirect_request": lambda *a: None}))
    try:
        opener.open(req)
    except urllib.error.HTTPError as e:
        assert e.code == 302 and e.headers["Location"] == "/login"
    assert _get(live, "/health", "GET", host)[0] == 200                              # Render's health check stays open
    assert _get(live, "/", "GET", {"Host": "evil.example"})[0] == 403
    body = b"password=nope"
    code, page = _get(live, "/login", "POST", {**host, "Content-Type": "application/x-www-form-urlencoded"}, body)
    assert code == 401 and "That password is not right." in page
    req = urllib.request.Request(f"http://127.0.0.1:{live.server_address[1]}/login", method="POST",
                                 data=b"password=correct+horse+battery",
                                 headers={**host, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        opener.open(req)
    except urllib.error.HTTPError as e:
        assert e.code == 302
        cookie = e.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" in cookie
    session = cookie.split(";")[0]
    assert "Start a run" in _get(live, "/", "GET", {**host, "Cookie": session})[1]
    tampered = session[:-1] + ("a" if session[-1] != "a" else "b")
    for bad in (tampered, "da_session=9999999999.forged", ""):                       # each one lands on sign-in
        page = _get(live, "/", "GET", {**host, "Cookie": bad})[1]
        assert "Sign in" in page and "Start a run" not in page


def test_sessions_expire_and_wrong_passwords_slow_down(live, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "correct horse battery")
    assert server.session_ok(server.make_session()) and not server.session_ok(server.make_session(now=0))
    server._failed_logins.clear()
    codes = [_get(live, "/login", "POST", {"Content-Type": "application/x-www-form-urlencoded"}, b"password=x")[0] for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429


def test_it_will_not_listen_publicly_without_a_password(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    with pytest.raises(SystemExit, match="APP_PASSWORD"):
        server.serve(0, 0, "0.0.0.0")
