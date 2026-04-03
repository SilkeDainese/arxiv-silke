"""Receive failure reports from fork workflows and create upstream issues."""

from __future__ import annotations

import base64
import json
import os
import re
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from http.server import BaseHTTPRequestHandler
from typing import Any

GITHUB_API = "https://api.github.com"
STORAGE_GITHUB_TOKEN = os.environ.get("STUDENT_STORAGE_GITHUB_TOKEN", "").strip()
STORAGE_REPO = os.environ.get("STUDENT_STORAGE_REPO", "").strip()
REPORT_PATH = os.environ.get("REPORT_STORAGE_PATH", "reports/failures.json").strip()
STORAGE_BRANCH = os.environ.get("STUDENT_STORAGE_BRANCH", "main").strip()
UPSTREAM_REPO = os.environ.get("UPSTREAM_REPO", "SilkeDainese/arxiv-digest").strip()
REPORT_RELAY_TOKEN = os.environ.get("REPORT_RELAY_TOKEN", "").strip()

REQUIRED_FIELDS = ("repo", "run_id", "error", "timestamp", "workflow")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}/[A-Za-z0-9_.\-]{1,100}$")
_MAX_STORE_ENTRIES = 500
_MAX_ERROR_LEN = 2000


def _github_request(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send a GitHub API request and decode the JSON response."""
    if not STORAGE_GITHUB_TOKEN or not STORAGE_REPO:
        raise RuntimeError(
            "Report storage is not configured. "
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
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _load_report_store() -> tuple[list[dict[str, Any]], str | None]:
    """Load the failures JSON from the private GitHub repo."""
    url = (
        f"{GITHUB_API}/repos/{STORAGE_REPO}/contents/"
        f"{quote(REPORT_PATH)}?ref={quote(STORAGE_BRANCH)}"
    )
    try:
        data = _github_request("GET", url)
    except HTTPError as exc:
        if exc.code == 404:
            return [], None
        raise

    content = base64.b64decode(data["content"]).decode("utf-8")
    store = json.loads(content) if content.strip() else []
    if not isinstance(store, list):
        store = []
    return store, data.get("sha")


def _save_report_store(store: list[dict[str, Any]], sha: str | None, message: str) -> None:
    """Persist the failure store back to GitHub."""
    content = json.dumps(store, indent=2).encode("utf-8")
    payload: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": STORAGE_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    url = f"{GITHUB_API}/repos/{STORAGE_REPO}/contents/{quote(REPORT_PATH)}"
    _github_request("PUT", url, payload)


def _sanitise_error(error: str) -> str:
    """Truncate and escape the error string so it can't break GitHub markdown."""
    truncated = error[:_MAX_ERROR_LEN]
    # Replace triple backticks to prevent breaking out of the code fence
    return truncated.replace("```", "'''")


def _create_issue(repo: str, run_id: str, error: str, workflow: str) -> str:
    """Create a GitHub issue on the upstream repo and return its URL."""
    run_link = f"https://github.com/{repo}/actions/runs/{run_id}"
    title = f"Digest failure: {repo}"
    body = (
        f"**Fork:** `{repo}`\n"
        f"**Workflow:** `{workflow}`\n"
        f"**Run:** {run_link}\n\n"
        f"```\n{_sanitise_error(error)}\n```"
    )
    url = f"{GITHUB_API}/repos/{UPSTREAM_REPO}/issues"
    result = _github_request("POST", url, {"title": title, "body": body})
    return result["html_url"]


def _handle_report(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Validate, store, and file an issue for a failure report."""
    # Optional token guard: if REPORT_RELAY_TOKEN is configured on the relay,
    # callers must supply it. Forks without it silently succeed with no storage/issue.
    if REPORT_RELAY_TOKEN:
        if str(body.get("token", "")).strip() != REPORT_RELAY_TOKEN:
            # Return 200 so fork workflows don't flag a failure on mis-config,
            # but do nothing — this prevents unauthenticated abuse when token is set.
            return 200, {"ok": True, "skipped": "token required"}

    missing = [f for f in REQUIRED_FIELDS if not str(body.get(f, "")).strip()]
    if missing:
        return 400, {"error": f"Missing required fields: {', '.join(missing)}"}

    repo = str(body["repo"]).strip()
    run_id = str(body["run_id"]).strip()
    error = str(body["error"]).strip()
    workflow = str(body["workflow"]).strip()
    timestamp = str(body["timestamp"]).strip()

    # Reject obviously invalid repo or run_id formats
    if not _REPO_RE.match(repo):
        return 400, {"error": "Invalid repo format (expected owner/name)."}
    if not re.match(r"^\d{1,20}$", run_id):
        return 400, {"error": "Invalid run_id format (expected numeric)."}

    store, sha = _load_report_store()

    # Deduplicate: don't create a second issue for the same (repo, run_id)
    if any(r.get("repo") == repo and r.get("run_id") == run_id for r in store):
        return 200, {"ok": True, "skipped": "already reported"}

    store.append({
        "repo": repo,
        "run_id": run_id,
        "error": error[:_MAX_ERROR_LEN],
        "workflow": workflow,
        "timestamp": timestamp,
    })
    # Keep the store bounded — drop oldest entries when over the cap
    if len(store) > _MAX_STORE_ENTRIES:
        store = store[-_MAX_STORE_ENTRIES:]
    _save_report_store(store, sha, f"Add failure report from {repo}")

    issue_url = _create_issue(repo, run_id, error, workflow)

    return 200, {"ok": True, "issue_url": issue_url}


class handler(BaseHTTPRequestHandler):
    """Vercel Python serverless handler for the failure report API."""

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, ValueError):
            self._respond(400, {"error": "invalid JSON"})
            return

        try:
            status, payload = _handle_report(body)
            self._respond(status, payload)
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
