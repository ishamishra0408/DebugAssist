"""Sign in with GitHub (Isha 2026-10-08, after the PM review: each person signs in with their own GitHub account; the
shared password is gone).

  GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET   a GitHub OAuth app (GitHub ▸ Settings ▸ Developer settings ▸ OAuth Apps)
                                           whose callback is https://<this address>/auth/github/callback
  ALLOWED_GITHUB_USERS                     the GitHub accounts that may sign in, comma-separated; anyone else is refused

It asks GitHub for no permissions (no scope): it learns who you are and nothing else, and keeps no GitHub token. A
session is a signed cookie naming the account, checked against the list on every request, so taking a name off the
list signs that person out at once. Without these settings: no sign-in on this Mac (as before); a public address stays
locked (server.locked_reason).
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.parse
import urllib.request

SESSION_S = 12 * 3600
STATE_S = 600
LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")   # GitHub's own rule for account names
AUTHORIZE = "https://github.com/login/oauth/authorize"
TOKEN = "https://github.com/login/oauth/access_token"
USER = "https://api.github.com/user"


class AuthError(Exception):
    """Sign-in did not complete; the reason is for the log, the page shows a fixed sentence."""


def client_id() -> str:
    return os.environ.get("GITHUB_CLIENT_ID", "").strip()


def _secret() -> str:
    return os.environ.get("GITHUB_CLIENT_SECRET", "").strip()


def allowed() -> set[str]:
    return {x.strip().lstrip("@").lower() for x in os.environ.get("ALLOWED_GITHUB_USERS", "").split(",") if x.strip()}


def configured() -> bool:
    return bool(client_id() and _secret() and allowed())


def missing() -> list[str]:
    return [k for k, v in (("GITHUB_CLIENT_ID", client_id()), ("GITHUB_CLIENT_SECRET", _secret()),
                           ("ALLOWED_GITHUB_USERS", ",".join(allowed()))) if not v]


def _key() -> bytes:
    return hashlib.sha256(b"da-session:" + _secret().encode()).digest()


def make_session(login: str, now: float | None = None) -> str:
    exp = str(int((time.time() if now is None else now) + SESSION_S))
    body = f"{login.lower()}.{exp}"
    return body + "." + hmac.new(_key(), body.encode(), "sha256").hexdigest()


def session_user(value: str, now: float | None = None) -> str:
    """The signed-in account, or "" (no cookie, forged, expired, or no longer on the list)."""
    login, _, rest = (value or "").partition(".")
    exp, _, sig = rest.partition(".")
    if not LOGIN.match(login) or not exp.isdigit() or int(exp) < (time.time() if now is None else now):
        return ""
    good = hmac.new(_key(), f"{login}.{exp}".encode(), "sha256").hexdigest()
    return login if hmac.compare_digest(sig, good) and login in allowed() else ""


def new_state(next_path: str) -> tuple[str, str]:
    """(state for GitHub, cookie value): the state is random; the cookie also carries where to go after."""
    state = secrets.token_urlsafe(24)
    nxt = base64.urlsafe_b64encode(safe_next(next_path).encode()).decode().rstrip("=")
    return state, f"{state}.{nxt}"


def check_state(cookie_value: str, state: str) -> str | None:
    """Where to go after sign-in when the state GitHub sent back is the one this browser started with; else None."""
    want, _, nxt = (cookie_value or "").partition(".")
    if not want or not state or not hmac.compare_digest(want, state):
        return None
    try:
        return safe_next(base64.urlsafe_b64decode(nxt + "=" * (-len(nxt) % 4)).decode())
    except ValueError:
        return "/"


def safe_next(path: str) -> str:
    """Only a path on this site (never //elsewhere or a full address)."""
    p = (path or "/").strip()
    return p if p.startswith("/") and not p.startswith("//") and "\\" not in p and len(p) < 300 else "/"


def authorize_url(state: str, redirect_uri: str) -> str:
    return AUTHORIZE + "?" + urllib.parse.urlencode({"client_id": client_id(), "redirect_uri": redirect_uri,
                                                    "state": state, "allow_signup": "false"})


def _post_json(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers={"Accept": "application/json", "User-Agent": "DebugAssistAgent"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "DebugAssistAgent",
                                               "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def account_for(code: str, redirect_uri: str) -> str:
    """GitHub's one-time code → the account name. The token is used once, here, and not kept."""
    if not code or len(code) > 200:
        raise AuthError("no code")
    try:
        got = _post_json(TOKEN, {"client_id": client_id(), "client_secret": _secret(), "code": code,
                                 "redirect_uri": redirect_uri})
        token = got.get("access_token")
        if not token:
            raise AuthError(f"GitHub gave no token ({got.get('error', 'no reason')})")
        login = str(_get_json(USER, token).get("login") or "")
    except (OSError, ValueError) as ex:
        raise AuthError(f"GitHub did not answer ({type(ex).__name__})") from ex
    if not LOGIN.match(login):
        raise AuthError("GitHub gave no account name")
    return login.lower()
