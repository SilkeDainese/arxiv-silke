"""Central anonymous feedback store for aggregate student ranking signals."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from typing import Any

GITHUB_API = "https://api.github.com"
STORAGE_GITHUB_TOKEN = os.environ.get("STUDENT_STORAGE_GITHUB_TOKEN", "").strip()
STORAGE_REPO = os.environ.get("STUDENT_STORAGE_REPO", "").strip()
FEEDBACK_PATH = os.environ.get("FEEDBACK_STORAGE_PATH", "feedback/votes.json").strip()
STORAGE_BRANCH = os.environ.get("STUDENT_STORAGE_BRANCH", "main").strip()
FEEDBACK_RELAY_TOKEN = os.environ.get("FEEDBACK_RELAY_TOKEN", "").strip()
STUDENT_ADMIN_TOKEN = os.environ.get("STUDENT_ADMIN_TOKEN", "").strip()


def _github_request(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send a GitHub API request and decode the JSON response."""
    if not STORAGE_GITHUB_TOKEN or not STORAGE_REPO:
        raise RuntimeError(
            "Feedback storage is not configured. "
            "Set STUDENT_STORAGE_GITHUB_TOKEN and STUDENT_STORAGE_REPO."
        )
    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {STORAGE_GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _load_feedback_store() -> tuple[dict[str, Any], str | None]:
    """Load the central feedback JSON from the private GitHub repo."""
    url = (
        f"{GITHUB_API}/repos/{STORAGE_REPO}/contents/"
        f"{urllib.parse.quote(FEEDBACK_PATH)}?ref={urllib.parse.quote(STORAGE_BRANCH)}"
    )
    try:
        data = _github_request("GET", url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"votes": [], "aggregated": {}}, None
        raise

    content = base64.b64decode(data["content"]).decode("utf-8")
    store = json.loads(content) if content.strip() else {"votes": [], "aggregated": {}}
    store.setdefault("votes", [])
    store.setdefault("aggregated", {})
    return store, data.get("sha")


def _save_feedback_store(store: dict[str, Any], sha: str | None, message: str) -> None:
    """Persist the feedback store back to GitHub."""
    content = json.dumps(store, indent=2, sort_keys=True).encode("utf-8")
    payload: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": STORAGE_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    url = f"{GITHUB_API}/repos/{STORAGE_REPO}/contents/{urllib.parse.quote(FEEDBACK_PATH)}"
    _github_request("PUT", url, payload)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _reaggregate(store: dict[str, Any]) -> None:
    """Rebuild the per-paper aggregate scores from raw votes."""
    agg: dict[str, dict[str, Any]] = {}
    for vote in store["votes"]:
        paper_id = vote.get("paper_id", "")
        if not paper_id:
            continue
        if paper_id not in agg:
            agg[paper_id] = {
                "up": 0,
                "down": 0,
                "net": 0,
                "keywords": {},
                "package_tags": {},
                "latest_vote": "",
            }
        entry = agg[paper_id]
        direction = vote.get("vote", "")
        if direction == "up":
            entry["up"] += 1
        elif direction == "down":
            entry["down"] += 1
        entry["net"] = entry["up"] - entry["down"]
        for kw in vote.get("keywords", []):
            entry["keywords"][kw] = entry["keywords"].get(kw, 0) + (1 if direction == "up" else -1)
        for tag in vote.get("package_tags", []):
            entry["package_tags"][tag] = entry["package_tags"].get(tag, 0) + 1
        ts = vote.get("timestamp", "")
        if ts > entry["latest_vote"]:
            entry["latest_vote"] = ts
    store["aggregated"] = agg


def _handle_submit(body: dict[str, Any]) -> dict[str, Any]:
    """Accept a batch of anonymised votes from a researcher digest run."""
    token = str(body.get("token", "")).strip()
    if not FEEDBACK_RELAY_TOKEN or token != FEEDBACK_RELAY_TOKEN:
        raise PermissionError("Invalid feedback relay token.")

    votes = body.get("votes", [])
    if not isinstance(votes, list) or not votes:
        raise ValueError("No votes provided.")
    if len(votes) > 200:
        raise ValueError("Too many votes in one batch (max 200).")

    store, sha = _load_feedback_store()
    timestamp = _now_iso()
    accepted = 0

    for vote in votes:
        paper_id = str(vote.get("paper_id", "")).strip()
        direction = str(vote.get("vote", "")).strip().lower()
        if not paper_id or direction not in ("up", "down"):
            continue
        keywords = [str(kw).strip().lower() for kw in (vote.get("keywords") or []) if str(kw).strip()][:10]
        package_tags = [str(t).strip().lower() for t in (vote.get("package_tags") or []) if str(t).strip()][:5]
        store["votes"].append({
            "paper_id": paper_id,
            "vote": direction,
            "keywords": keywords,
            "package_tags": package_tags,
            "timestamp": timestamp,
        })
        accepted += 1

    if accepted == 0:
        raise ValueError("No valid votes in the batch.")

    _reaggregate(store)
    _save_feedback_store(store, sha, f"Add {accepted} feedback vote(s)")
    return {"ok": True, "accepted": accepted}


def _handle_aggregate(body: dict[str, Any]) -> dict[str, Any]:
    """Return the aggregate vote data for the student digest ranker."""
    token = str(body.get("admin_token", "")).strip()
    if not STUDENT_ADMIN_TOKEN or token != STUDENT_ADMIN_TOKEN:
        raise PermissionError("Invalid admin token.")

    store, _ = _load_feedback_store()
    return {
        "ok": True,
        "aggregated": store.get("aggregated", {}),
        "total_votes": len(store.get("votes", [])),
    }


def _handle_stats(body: dict[str, Any]) -> dict[str, Any]:
    """Return summary stats about the feedback store."""
    token = str(body.get("admin_token", "")).strip()
    if not STUDENT_ADMIN_TOKEN or token != STUDENT_ADMIN_TOKEN:
        raise PermissionError("Invalid admin token.")

    store, _ = _load_feedback_store()
    votes = store.get("votes", [])
    aggregated = store.get("aggregated", {})
    return {
        "ok": True,
        "total_votes": len(votes),
        "unique_papers": len(aggregated),
        "papers_with_positive_net": sum(1 for v in aggregated.values() if v.get("net", 0) > 0),
        "papers_with_negative_net": sum(1 for v in aggregated.values() if v.get("net", 0) < 0),
    }


def _dispatch(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    action = str(body.get("action", "")).strip().lower()
    if action == "submit":
        return 200, _handle_submit(body)
    if action == "aggregate":
        return 200, _handle_aggregate(body)
    if action == "stats":
        return 200, _handle_stats(body)
    return 400, {"error": "unknown action"}


class handler(BaseHTTPRequestHandler):
    """Vercel Python serverless handler for the feedback API."""

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, ValueError):
            self._respond(400, {"error": "invalid JSON"})
            return

        try:
            status, payload = _dispatch(body)
            self._respond(status, payload)
        except PermissionError as exc:
            self._respond(403, {"error": str(exc)})
        except ValueError as exc:
            self._respond(400, {"error": str(exc)})
        except Exception as exc:
            self._respond(500, {"error": str(exc)})

    def _respond(self, status: int, body: dict[str, Any]):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        pass
