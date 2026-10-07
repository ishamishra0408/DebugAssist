"""Read-only GitHub access. The pipeline holds no write credential, so it cannot publish by construction."""
import json
import re
import urllib.error
import urllib.request

from .config import CFG

ISSUE_URL = re.compile(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)")


def parse_issue_url(url: str) -> tuple[str, str, int]:
    m = ISSUE_URL.search(url)
    if not m:
        raise ValueError(f"not a GitHub issue URL: {url}")
    return m.group(1), m.group(2), int(m.group(3))


def api(path: str, accept: str = "application/vnd.github+json"):
    """GET one read-only GitHub REST path (e.g. "repos/vercel/ai/commits?path=x"). None on 404."""
    req = urllib.request.Request(f"https://api.github.com/{path.lstrip('/')}", method="GET",
                                 headers={"Authorization": f"Bearer {CFG.github_token}", "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (404, 422):
            return None
        raise


def get_issue(url: str) -> dict:
    owner, repo, number = parse_issue_url(url)
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{number}",
        headers={"Authorization": f"Bearer {CFG.github_token}", "Accept": "application/vnd.github+json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    return {"owner": owner, "repo": repo, "number": number, "url": d["html_url"], "title": d["title"],
            "body": d.get("body") or "", "labels": [l["name"] for l in d.get("labels", [])],
            "reporter": d["user"]["login"], "state": d["state"], "created_at": d.get("created_at"),
            "comments": d.get("comments", 0)}
