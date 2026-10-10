import shutil
import subprocess

import pytest

TEST_DB = "debug_assist_test"


def _mongo_up() -> bool:
    try:
        from pymongo import MongoClient

        from debug_assist.config import CFG
        return bool(MongoClient(CFG.mongodb_uri, serverSelectionTimeoutMS=1500).admin.command("ping"))
    except Exception:
        return False


MONGO_UP = _mongo_up()
DOCKER_UP = bool(shutil.which("docker")) and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.fixture(autouse=True)
def no_inherited_git_env(monkeypatch):
    """Tests that make throwaway git repos must never write into an outer repo's index (a commit hook sets these)."""
    for k in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE", "GIT_OBJECT_DIRECTORY", "GIT_PREFIX"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def sign_in_off(monkeypatch):
    """Tests start with GitHub sign-in off and no public address, whatever .env says (2026-10-08: with the GitHub
    settings in .env for a local try, every page test landed on sign-in). A test that needs them sets its own."""
    for k in ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "ALLOWED_GITHUB_USERS", "PUBLIC_HOST", "RENDER_EXTERNAL_HOSTNAME"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def no_fetching_main(monkeypatch):
    """Tests never ask GitHub for main's newest commit (2026-10-10: a test reached the real vercel/ai). A run then uses
    the saved copy and says so; a test of the fetch sets its own."""
    from debug_assist import fresh
    def offline(profile):
        raise RuntimeError("offline in tests")
    monkeypatch.setattr(fresh, "latest", offline)


@pytest.fixture(autouse=True)
def no_profile_extras(monkeypatch):
    """Tests see the profiles as written, never packages chosen on this Mac (pkgcheck.extras reads MongoDB)."""
    from debug_assist import pkgcheck
    monkeypatch.setattr(pkgcheck, "extras", lambda repo: [])


@pytest.fixture(autouse=True)
def advisors_off(monkeypatch, tmp_path):
    """Tests never reach the real advisors or write to the real bundle, whatever .env says (2026-10-08: with the
    advisors switched on in .env, a test could have asked the live server). A test that needs them sets its own."""
    import dataclasses

    from debug_assist import advisors, config
    off = dataclasses.replace(config.CFG, advisors_mcp="", advisors_reviewed=False, bundle_dir=tmp_path / "bundle")
    monkeypatch.setattr(advisors, "CFG", off)
    monkeypatch.setattr(config, "CFG", off)
    monkeypatch.delenv("ADVISORS_KEY", raising=False)


@pytest.fixture
def scratch_db(monkeypatch):
    """The meter and the event log, pointed at a throwaway database (the real one is never touched)."""
    if not MONGO_UP:
        pytest.skip("MongoDB not running")
    from debug_assist import events, meter
    from debug_assist.store import client
    client().drop_database(TEST_DB)
    monkeypatch.setattr(meter, "_db_name", TEST_DB)
    monkeypatch.setattr(events, "_db_name", TEST_DB)
    yield client()[TEST_DB]
    client().drop_database(TEST_DB)
