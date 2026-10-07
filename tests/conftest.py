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
