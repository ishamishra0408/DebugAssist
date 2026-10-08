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
    monkeypatch.setattr("debug_assist.store.reachable", lambda ttl_s=15.0: True)
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
    assert "<form" not in page and page.count('method: "POST"') == 1 and 'fetch("/api/decide"' in page  # only your OK posts
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


def test_a_public_address_without_its_password_stays_locked_and_says_why(monkeypatch, tmp_path):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "debugassistagent.onrender.com")
    assert "APP_PASSWORD" in server.locked_reason("0.0.0.0") and server.locked_reason("127.0.0.1") == ""
    httpd = server.make(0)
    httpd.locked = server.locked_reason("0.0.0.0")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert _get(httpd, "/health")[0] == 200                       # Render sees it alive, so the deploy finishes
        code, text = _get(httpd, "/")
        assert code == 503 and "is locked" in text and "APP_PASSWORD" in text
        assert _get(httpd, "/api/start", "POST", {"Content-Type": "application/json"}, b"{}")[0] == 503
    finally:
        httpd.shutdown()
    monkeypatch.setenv("APP_PASSWORD", "correct horse battery")
    assert server.locked_reason("0.0.0.0") == ""                      # Render's own hostname counts as the address


def test_the_system_check_shows_each_service_in_plain_words(live, monkeypatch):
    from debug_assist import preflight, profiles
    monkeypatch.setattr(profiles, "ready", lambda: ["vercel/ai"])
    monkeypatch.setattr(preflight, "run_preflight", lambda url, **k: [
        preflight.Check("MongoDB", "PASS", "writable primary"),
        preflight.Check("Embeddings", "FAIL", "VOYAGE_API_KEY not set", "add it to the host's environment yourself")])
    code, page = _get(live, "/checks")
    assert code == 200 and "Database (MongoDB)" in page and "Working: writable primary" in page
    assert "Embeddings (Voyage)" in page and "Not working: VOYAGE_API_KEY not set" in page and "To fix:" in page
    import html
    assert "1 thing is not working. A run can't start until it is fixed." in html.unescape(page)


def test_a_repo_is_connected_like_the_terminal_connects_it_and_only_one_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CFG", SimpleNamespace(runs_dir=tmp_path))
    monkeypatch.setattr("debug_assist.profiles.ready", lambda: ["vercel/ai", "acme/done"])
    calls = []

    class Proc:
        def __init__(self, argv, **kw):
            calls.append(argv)

        def poll(self):
            return None
    monkeypatch.setattr(server.subprocess, "Popen", Proc)
    monkeypatch.setitem(server._connector, "proc", None)
    monkeypatch.setitem(server._connector, "repo", None)
    monkeypatch.setattr(server, "_database_problem", lambda: "The database is not reachable, so the connection could not be saved.")
    with pytest.raises(server.Refused, match="database is not reachable"):       # never a silent crash (2026-10-08)
        server.start_connect("https://github.com/acme/widgets")
    assert not calls
    monkeypatch.setattr(server, "_database_problem", lambda: "")
    assert server.start_connect("https://github.com/acme/widgets.git") == "acme/widgets"
    assert calls[0][2:] == ["debug_assist", "connect", "https://github.com/acme/widgets"]
    assert (tmp_path / "_connect" / "acme-widgets.log").exists() and not server.RUN_ID.match("_connect")  # not a run
    with pytest.raises(server.Refused, match="being connected"):
        server.start_connect("https://github.com/acme/other")
    monkeypatch.setitem(server._connector, "proc", None)
    with pytest.raises(server.Refused, match="already connected"):
        server.start_connect("https://github.com/acme/done")
    assert server.start_connect("https://github.com/acme/done", again=True) == "acme/done"   # its latest code
    monkeypatch.setitem(server._connector, "proc", None)
    with pytest.raises(server.Refused, match="already set up"):
        server.start_connect("https://github.com/vercel/ai", again=True)
    with pytest.raises(server.Refused, match="not a GitHub repository"):
        server.start_connect("https://gitlab.com/acme/widgets")


def test_the_connect_page_shows_each_step_live_and_only_the_page_can_start_it(live, monkeypatch):
    doc = {"_id": "acme/widgets", "status": "running", "log": ["Step 3/7: RUN uv sync"], "steps": [
        {"key": "read", "label": "Read the repo", "status": "done", "detail": "main at fffffff"},
        {"key": "detect", "label": "Work out its setup", "status": "done", "detail": "package manager uv"},
        {"key": "build", "label": "Build its test sandbox", "status": "running", "detail": "usually 5 to 15 minutes"},
        {"key": "prove", "label": "Run its tests", "status": "waiting", "detail": ""}]}
    monkeypatch.setattr("debug_assist.connect.status", lambda repo: doc if repo == "acme/widgets" else None)
    monkeypatch.setattr("debug_assist.profiles.ready", lambda: [])
    monkeypatch.setitem(server._connector, "repo", None)
    code, page = _get(live, "/connect?repo=acme/widgets")
    assert code == 200 and "Connecting acme/widgets" in page and 'data-k="progress"' in page and '<section class="group" data-final' not in page
    assert "package manager uv" in page and "RUN uv sync" in page and "Not started" in page
    doc["status"], doc["why"] = "failed", "Build its test sandbox: E2B build failed"
    assert '<section class="group" data-final="1"' in _get(live, "/connect?repo=acme/widgets")[1]
    assert "Connecting" not in _get(live, "/connect?repo=../../etc")[1]            # a bad name reads nothing
    started = []
    monkeypatch.setattr(server, "start_connect", lambda u, again=False: started.append((u, again)) or "acme/widgets")
    body = json.dumps({"url": "https://github.com/acme/widgets"}).encode()
    assert _get(live, "/api/connect", "POST", {"Content-Type": "application/json"}, body)[0] == 403 and not started
    code, out = _get(live, "/api/connect", "POST", _ok_headers(live), body)
    assert code == 200 and json.loads(out)["page"] == "/connect?repo=acme/widgets" and started


def test_a_connection_that_ends_before_saving_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CFG", SimpleNamespace(runs_dir=tmp_path))
    (tmp_path / "_connect").mkdir()
    (tmp_path / "_connect" / "acme-w.log").write_text("connecting acme/w\nNothing was saved: ServerSelectionTimeoutError: refused\n")
    monkeypatch.setitem(server._connector, "repo", "acme/w")
    monkeypatch.setitem(server._connector, "proc", SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr("debug_assist.connect.status", lambda repo: None)
    assert "Starting." in server._connect_progress("acme/w")
    monkeypatch.setitem(server._connector, "proc", SimpleNamespace(poll=lambda: 1))
    out = server._connect_progress("acme/w")
    assert 'data-final="1"' in out and "Could not connect acme/w. Nothing was saved: ServerSelectionTimeoutError" in out


def test_with_the_database_off_pages_answer_at_once_and_say_so(tmp_path, monkeypatch):
    import time
    (tmp_path / "ai-1-20261007-010101").mkdir()
    (tmp_path / "ai-1-20261007-010101" / "console.log").write_text("x")
    (tmp_path / "not-a-run").mkdir()
    monkeypatch.setattr(server, "CFG", SimpleNamespace(runs_dir=tmp_path))
    monkeypatch.setattr("debug_assist.store.reachable", lambda ttl_s=15.0: False)
    monkeypatch.setattr(server, "_status_of", lambda rid: pytest.fail("read a run's state with the database off"))
    t0 = time.monotonic()
    page = server.home_page()
    assert time.monotonic() - t0 < 1 and "Status unavailable: the database is off" in page and "not-a-run" not in page
    httpd = server.make(0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        code, text = _get(httpd, "/run/ai-1-20261007-010101")
        assert code == 503 and "The database is off" in text
    finally:
        httpd.shutdown()


def test_the_connect_page_has_a_card_per_advisor_and_the_steps_to_connect_them(monkeypatch):
    """Four advisors without the bloat (Isha 2026-10-08): a step track showing where each is asked, a rail of four
    tiles, one panel for the one picked; the steps to connect them, each ticked on its own facts, folded once done."""
    import dataclasses
    import re
    from debug_assist import advisors
    monkeypatch.setattr("debug_assist.profiles.ready", lambda: [])
    monkeypatch.setitem(server._connector, "repo", None)
    monkeypatch.delenv("ADVISORS_KEY", raising=False)

    def section(mcp="", reviewed=False, key=None):
        cfg = dataclasses.replace(advisors.CFG, advisors_mcp=mcp, advisors_reviewed=reviewed)
        monkeypatch.setattr(advisors, "CFG", cfg)
        monkeypatch.setattr("debug_assist.config.CFG", cfg)
        if key:
            monkeypatch.setenv("ADVISORS_KEY", key)
        return server.connect_page().split('id="advisors"')[1]
    c = section()
    assert re.findall(r'class="adv-tile[^"]*"[^>]*data-seat="([^"]+)"', c) == ["defect-triage", "cause-locator", "allspaw", "qe-ic-advisor"]
    marks = re.findall(r'<li class="adv-at"><button[^>]*data-seat="([^"]+)"[^>]*></button><span class="n">(\d+)</span>', c)
    assert marks == [("defect-triage", "1"), ("cause-locator", "4"), ("allspaw", "6"), ("qe-ic-advisor", "7")]
    assert c.count('role="tabpanel" data-seat=') == 4 and len(re.findall(r'role="tabpanel" data-seat="[^"]+" hidden', c)) == 3
    assert "After step 1, Read the issue" in c and "After step 4, Find the cause" in c and "Where&#x27;s the cause?" in c
    assert "Real defect or not?" in c and "Conditions, not culprits." in c and "Ship or stop." in c
    assert all(f'data-palette="{p}"' in c for p in ("tide", "violet", "ember", "moss"))
    assert "Not connected." in c and "1 of 4 done" in c and '<details class="adv-connect" open>' in c
    assert "<form" not in c and 'class="adv-ask"' not in c                                   # off: nothing to ask
    c = section(mcp="https://advisors.example/mcp/")
    assert "has not been marked reviewed" in c and "Waiting for review" in c and "2 of 4 done" in c
    c = section(mcp="https://advisors.example/mcp/", reviewed=True)
    assert "no key is set, so the server will refuse" in c and "No key yet" in c and "3 of 4 done" in c
    c = section(mcp="https://advisors.example/mcp/", reviewed=True, key="k")
    assert "4 of 4 done" in c and '<details class="adv-connect">' in c and ">On<" in c       # folded away once done
    assert "Set.</span>" in c and c.count('class="adv-ask"') == 4


def test_each_advisor_can_be_asked_by_hand_with_empty_boxes_and_a_hint(live, monkeypatch):
    """Each advisor's panel has its own empty boxes (as that advisor reads them) and a hint of when it helps; the
    answer appears under it. Asked by hand = consumer "operator"."""
    import dataclasses
    from debug_assist import advisors
    monkeypatch.setattr("debug_assist.profiles.ready", lambda: [])
    monkeypatch.setitem(server._connector, "repo", None)
    code, out = _get(live, "/api/advisors-ask", "POST", _ok_headers(live), b'{"seat": "allspaw", "fields": {}}')
    assert code == 400 and "not switched on" in json.loads(out)["error"]
    cfg = dataclasses.replace(advisors.CFG, advisors_mcp="https://advisors.example/mcp/", advisors_reviewed=True)
    monkeypatch.setattr(advisors, "CFG", cfg)
    monkeypatch.setattr("debug_assist.config.CFG", cfg)
    monkeypatch.setenv("ADVISORS_KEY", "k")
    page = server.connect_page().split('id="advisors"')[1]
    assert page.count('class="adv-ask"') == 4 and 'value="' not in page                           # empty boxes
    assert 'name="repo"' in page and 'name="files"' in page and 'placeholder="Paste the stack trace or the failed assertion"' in page
    asked = []
    monkeypatch.setattr(advisors, "_call", lambda seat, q, ev, consumer="debugassist", context=None, numbers=None: (
        asked.append((seat, q, ev, consumer, context)), "said")[1])

    def post(body):
        return _get(live, "/api/advisors-ask", "POST", _ok_headers(live), json.dumps(body).encode())
    assert _get(live, "/api/advisors-ask", "POST", {"Content-Type": "application/json"}, b"{}")[0] == 403   # not from the page
    code, out = post({"seat": "allspaw", "fields": {"q": "The fix: x", "e": "The report."}})
    assert code == 200 and json.loads(out) == {"said": "said"} and asked[-1] == ("allspaw", "The fix: x", "The report.", "operator", None)
    code, out = post({"seat": "defect-triage", "fields": {"title": "T", "body": "B", "repo": "acme/x"}})
    assert code == 200 and asked[-1] == ("defect-triage", "T", "B", "operator", {"repo_name": "acme/x"})
    code, out = post({"seat": "cause-locator", "fields": {"desc": "D", "repro": "AssertionError", "files": "a.ts\nb.ts\n"}})
    ctx = asked[-1][4]
    assert code == 200 and ctx["repo_listing"] == ["a.ts", "b.ts"] and ctx["repro_output"] == "AssertionError"
    assert [c["path"] for c in ctx["candidates"]] == ["a.ts", "b.ts"]
    code, out = post({"seat": "defect-triage", "fields": {"title": "T", "body": "", "repo": ""}})
    assert code == 400 and "fill in every box: issue text, repository" in json.loads(out)["error"].lower()
    code, out = post({"seat": "nobody", "fields": {}})
    assert code == 400 and "no such advisor" in json.loads(out)["error"].lower()


def test_the_advisor_scenes_are_served_and_degrade_quietly():
    js = (viewer.STATIC / "advisors.js").read_text()
    assert server.STATIC_FILES["advisors.js"].startswith("text/javascript")
    assert "prefers-reduced-motion" in js and "IntersectionObserver" in js and "no-gl" in js      # still, paused, fallback
    assert 'addEventListener("advisor-state"' in js and "window.gsap" in js                       # works without GSAP too
    page = server.connect_page()
    assert "/static/advisors.js" in page and "gsap/3.12.5/gsap.min.js" in page


def test_your_ok_from_the_page_runs_the_terminals_command_bound_to_the_text_shown(tmp_path, monkeypatch):
    from debug_assist.guardrails import fingerprint
    (tmp_path / "ai-1-x").mkdir()
    pr = tmp_path / "ai-1-x" / "PR.md"
    pr.write_text("## Fix\nthe text you read")
    sha = fingerprint(pr.read_text())
    monkeypatch.setattr(server, "CFG", SimpleNamespace(runs_dir=tmp_path))
    monkeypatch.setattr("debug_assist.artifacts.enabled", lambda: False)
    waiting = SimpleNamespace(tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value={"sha256": sha, "pr_body_path": str(pr)})])])
    state = {"snap": waiting}
    monkeypatch.setattr(viewer, "_app", lambda: SimpleNamespace(get_state=lambda cfg: state["snap"]))
    calls = []

    class Proc:
        def __init__(self, argv, **kw):
            calls.append(argv)

        def poll(self):
            return 0
    monkeypatch.setattr(server.subprocess, "Popen", Proc)
    monkeypatch.setitem(server._decider, "proc", None)
    with pytest.raises(server.Refused, match="approve or say no"):
        server.decide("ai-1-x", "maybe", sha)
    with pytest.raises(server.Refused, match="changed since this page was opened"):
        server.decide("ai-1-x", "approve", "0" * 64)
    assert "Approved" in server.decide("ai-1-x", "approve", sha)
    assert calls[-1][2:] == ["debug_assist", "approve", "ai-1-x", "--no-view"]           # exactly the terminal's command
    assert "said no" in server.decide("ai-1-x", "reject", sha) and calls[-1][3] == "reject"
    pr.write_text("## Fix\nedited after you read it")
    with pytest.raises(server.Refused, match="not the text you were shown"):
        server.decide("ai-1-x", "approve", sha)
    state["snap"] = SimpleNamespace(tasks=[])
    with pytest.raises(server.Refused, match="not waiting for your OK"):
        server.decide("ai-1-x", "approve", sha)
    with pytest.raises(server.Refused, match="No such run"):
        server.decide("../etc", "approve", sha)


def test_the_problem_is_picked_from_the_issue_and_the_list_shows_only_when_it_holds_more_than_one():
    """Isha 2026-10-08 (#22288): title, description and reproduction are one problem; don't ask which."""
    one = [{"heading": "Description", "preview": "After the resume the message holds two text parts"},
           {"heading": "Reproduction", "preview": "pnpm add ai@7"}]
    texts = {"Description": "the replayed `text-start` chunk adds a second text part", "Reproduction": "```sh\npnpm add ai\n```"}
    got = server.pick_focus("Chat.resumeStream duplicates text parts", one, texts)
    assert got == {"single": True, "heading": "Description", "preview": one[0]["preview"], "from": "the Description section"}
    two = one + [{"heading": "Secondary observation", "preview": "Also, the gateway drops Error.message"}]
    assert server.pick_focus("t", two, {**texts, "Secondary observation": "x"})["single"] is False   # #21439's shape
    plain = [{"heading": "Description", "preview": "it breaks"}]
    assert server.pick_focus("The title", plain, {"Description": "it breaks"}) == {
        "single": True, "heading": "", "preview": "The title", "from": "the title"}            # no code quoted: the title
