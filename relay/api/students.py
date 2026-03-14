"""Central student subscription management API for AU student digests."""

from __future__ import annotations

import base64
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from student_registry import (  # noqa: E402
    DEFAULT_MAX_PAPERS,
    build_student_record,
    now_iso,
    normalise_email,
    package_labels,
    public_record,
    verify_password,
)

GITHUB_API = "https://api.github.com"
STORAGE_GITHUB_TOKEN = os.environ.get("STUDENT_STORAGE_GITHUB_TOKEN", "").strip()
STORAGE_REPO = os.environ.get("STUDENT_STORAGE_REPO", "").strip()
STORAGE_PATH = os.environ.get("STUDENT_STORAGE_PATH", "students/subscriptions.json").strip()
STORAGE_BRANCH = os.environ.get("STUDENT_STORAGE_BRANCH", "main").strip()
STUDENT_ADMIN_TOKEN = os.environ.get("STUDENT_ADMIN_TOKEN", "").strip()


def _github_request(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send a GitHub API request and decode the JSON response."""
    if not STORAGE_GITHUB_TOKEN or not STORAGE_REPO:
        raise RuntimeError(
            "Student registry storage is not configured. "
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


def _load_registry() -> tuple[dict[str, Any], str | None]:
    """Load the student registry JSON file from the private GitHub repo."""
    url = (
        f"{GITHUB_API}/repos/{STORAGE_REPO}/contents/"
        f"{urllib.parse.quote(STORAGE_PATH)}?ref={urllib.parse.quote(STORAGE_BRANCH)}"
    )
    try:
        data = _github_request("GET", url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"students": {}}, None
        raise

    content = base64.b64decode(data["content"]).decode("utf-8")
    registry = json.loads(content) if content.strip() else {"students": {}}
    registry.setdefault("students", {})
    return registry, data.get("sha")


def _save_registry(registry: dict[str, Any], sha: str | None, message: str) -> None:
    """Persist the registry JSON file back to GitHub."""
    content = json.dumps(registry, indent=2, sort_keys=True).encode("utf-8")
    payload: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": STORAGE_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    url = f"{GITHUB_API}/repos/{STORAGE_REPO}/contents/{urllib.parse.quote(STORAGE_PATH)}"
    _github_request("PUT", url, payload)


def _require_admin_token(token: str) -> None:
    """Validate the admin token used by the batch sender."""
    if not STUDENT_ADMIN_TOKEN or token != STUDENT_ADMIN_TOKEN:
        raise PermissionError("Invalid admin token.")


def _handle_upsert(body: dict[str, Any]) -> dict[str, Any]:
    email = normalise_email(body.get("email", ""))
    registry, sha = _load_registry()
    existing = registry["students"].get(email)
    record = build_student_record(
        email=email,
        password=str(body.get("password", "")),
        new_password=str(body.get("new_password", "")),
        package_ids=body.get("package_ids", []),
        max_papers_per_week=body.get("max_papers_per_week", DEFAULT_MAX_PAPERS),
        existing=existing,
    )
    registry["students"][email] = record
    _save_registry(registry, sha, f"Update student subscription for {email}")
    return {"ok": True, "subscription": public_record(record), "package_labels": package_labels()}


def _handle_get(body: dict[str, Any]) -> dict[str, Any]:
    email = normalise_email(body.get("email", ""))
    registry, _ = _load_registry()
    record = registry["students"].get(email)
    if not record:
        raise FileNotFoundError("Subscription not found.")
    if not verify_password(str(body.get("password", "")), record.get("password_salt", ""), record.get("password_hash", "")):
        raise PermissionError("Incorrect password.")
    return {"ok": True, "subscription": public_record(record), "package_labels": package_labels()}


def _handle_unsubscribe(body: dict[str, Any]) -> dict[str, Any]:
    email = normalise_email(body.get("email", ""))
    registry, sha = _load_registry()
    record = registry["students"].get(email)
    if not record:
        raise FileNotFoundError("Subscription not found.")
    if not verify_password(str(body.get("password", "")), record.get("password_salt", ""), record.get("password_hash", "")):
        raise PermissionError("Incorrect password.")
    record = dict(record)
    record["active"] = False
    record["updated_at"] = now_iso()
    registry["students"][email] = record
    _save_registry(registry, sha, f"Unsubscribe student {email}")
    return {"ok": True, "subscription": public_record(record), "package_labels": package_labels()}


def _handle_admin_list(body: dict[str, Any]) -> dict[str, Any]:
    _require_admin_token(str(body.get("admin_token", "")))
    include_inactive = bool(body.get("include_inactive", False))
    registry, _ = _load_registry()
    students = [
        public_record(record)
        for _, record in sorted(registry["students"].items())
        if include_inactive or record.get("active", True)
    ]
    return {"ok": True, "subscriptions": students, "package_labels": package_labels()}


def _dispatch(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    action = str(body.get("action", "")).strip().lower()
    if action == "upsert":
        return 200, _handle_upsert(body)
    if action == "get":
        return 200, _handle_get(body)
    if action == "unsubscribe":
        return 200, _handle_unsubscribe(body)
    if action == "admin_list":
        return 200, _handle_admin_list(body)
    return 400, {"error": "unknown action"}


def _manage_page(email: str, mode: str) -> str:
    """Return a simple browser-based student subscription management page."""
    safe_email = html.escape(email)
    packages_markup = "\n".join(
        f"""
        <label class="package">
          <input type="checkbox" name="package_ids" value="{html.escape(package_id)}">
          <span>{html.escape(label)}</span>
        </label>
        """
        for package_id, label in package_labels().items()
    )
    status_text = (
        "Enter your password and click Unsubscribe."
        if mode == "unsubscribe"
        else "Enter your password to load or update your student packages."
    )
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>AU student subscription</title>
    <style>
      :root {{
        --bg: #f6f1e8;
        --panel: #fffdf8;
        --text: #1f2933;
        --muted: #5f6c76;
        --accent: #1d5b57;
        --danger: #8a3b12;
        --border: #d8d1c6;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100vh;
        background: linear-gradient(180deg, var(--bg), #efe6d7);
        color: var(--text);
        font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
        padding: 24px;
      }}
      main {{
        width: min(100%, 720px);
        margin: 0 auto;
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 18px;
        padding: 28px;
        box-shadow: 0 18px 48px rgba(31, 41, 51, 0.08);
      }}
      h1 {{
        margin: 0 0 10px;
        font-size: clamp(2rem, 5vw, 2.6rem);
        line-height: 1.05;
      }}
      p, label, button, input {{
        font-size: 1rem;
      }}
      .muted {{
        color: var(--muted);
        line-height: 1.6;
      }}
      .stack {{
        display: grid;
        gap: 14px;
        margin-top: 18px;
      }}
      input[type="email"], input[type="password"], input[type="number"] {{
        width: 100%;
        padding: 12px 14px;
        border-radius: 10px;
        border: 1px solid var(--border);
        background: white;
      }}
      .packages {{
        display: grid;
        gap: 10px;
        grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      }}
      .package {{
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 12px 14px;
        border: 1px solid var(--border);
        border-radius: 12px;
        background: white;
      }}
      .actions {{
        display: flex;
        gap: 12px;
        flex-wrap: wrap;
      }}
      button {{
        border: 0;
        border-radius: 999px;
        padding: 12px 18px;
        background: var(--accent);
        color: white;
        cursor: pointer;
      }}
      button.secondary {{
        background: white;
        color: var(--accent);
        border: 1px solid var(--accent);
      }}
      button.danger {{
        background: var(--danger);
      }}
      .status {{
        margin-top: 16px;
        padding: 12px 14px;
        border-radius: 12px;
        background: rgba(29, 91, 87, 0.08);
        color: var(--accent);
        min-height: 48px;
      }}
    </style>
  </head>
  <body>
    <main>
      <h1>Manage your AU student digest</h1>
      <p class="muted">{html.escape(status_text)}</p>
      <div class="stack">
        <label>Email
          <input id="email" type="email" value="{safe_email}" placeholder="student@post.au.dk">
        </label>
        <label>Password
          <input id="password" type="password" placeholder="Your student digest password">
        </label>
        <label>New password (optional)
          <input id="new_password" type="password" placeholder="Leave blank to keep your current password">
        </label>
        <label>Max papers per week
          <input id="max_papers" type="number" min="1" max="20" value="{DEFAULT_MAX_PAPERS}">
        </label>
        <div>
          <strong>Packages</strong>
          <div class="packages">
            {packages_markup}
          </div>
        </div>
        <div class="actions">
          <button class="secondary" type="button" onclick="loadCurrent()">Load current settings</button>
          <button type="button" onclick="saveSubscription()">Save packages</button>
          <button class="danger" type="button" onclick="unsubscribe()">Unsubscribe</button>
        </div>
        <div id="status" class="status"></div>
      </div>
    </main>
    <script>
      const mode = new URLSearchParams(window.location.search).get("mode") || "";
      const statusEl = document.getElementById("status");

      function selectedPackages() {{
        return Array.from(document.querySelectorAll('input[name="package_ids"]:checked'))
          .map((input) => input.value);
      }}

      function setPackages(packageIds) {{
        const wanted = new Set(packageIds || []);
        document.querySelectorAll('input[name="package_ids"]').forEach((input) => {{
          input.checked = wanted.has(input.value);
        }});
      }}

      function setStatus(message, isError = false) {{
        statusEl.textContent = message;
        statusEl.style.background = isError ? "rgba(138, 59, 18, 0.08)" : "rgba(29, 91, 87, 0.08)";
        statusEl.style.color = isError ? "var(--danger)" : "var(--accent)";
      }}

      async function callApi(action) {{
        const payload = {{
          action,
          email: document.getElementById("email").value,
          password: document.getElementById("password").value,
          new_password: document.getElementById("new_password").value,
          max_papers_per_week: Number(document.getElementById("max_papers").value || "{DEFAULT_MAX_PAPERS}"),
          package_ids: selectedPackages(),
        }};
        const response = await fetch(window.location.pathname, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify(payload),
        }});
        const data = await response.json();
        if (!response.ok || !data.ok) {{
          throw new Error(data.error || "Request failed");
        }}
        return data;
      }}

      async function loadCurrent() {{
        setStatus("Loading current settings...");
        try {{
          const data = await callApi("get");
          document.getElementById("max_papers").value = data.subscription.max_papers_per_week;
          setPackages(data.subscription.package_ids);
          setStatus("Loaded current settings.");
        }} catch (error) {{
          setStatus(error.message, true);
        }}
      }}

      async function saveSubscription() {{
        setStatus("Saving subscription...");
        try {{
          const data = await callApi("upsert");
          setPackages(data.subscription.package_ids);
          document.getElementById("max_papers").value = data.subscription.max_papers_per_week;
          document.getElementById("new_password").value = "";
          setStatus("Saved. Your next weekly digest will use these packages.");
        }} catch (error) {{
          setStatus(error.message, true);
        }}
      }}

      async function unsubscribe() {{
        if (!confirm("Stop sending the AU student digest to this email?")) {{
          return;
        }}
        setStatus("Unsubscribing...");
        try {{
          await callApi("unsubscribe");
          setStatus("Unsubscribed. You can re-enable it later by saving packages again.");
        }} catch (error) {{
          setStatus(error.message, true);
        }}
      }}

      if (mode === "unsubscribe") {{
        setStatus("Enter your password and click Unsubscribe.");
      }} else {{
        setStatus("Enter your password, then load or save your package choices.");
      }}
    </script>
  </body>
</html>"""


class handler(BaseHTTPRequestHandler):
    """Vercel Python serverless handler."""

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        email = query.get("email", [""])[0]
        mode = query.get("mode", [""])[0]
        page = _manage_page(email, mode)
        payload = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

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
        except FileNotFoundError as exc:
            self._respond(404, {"error": str(exc)})
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
        """Suppress default stderr logging."""
        pass
