"""Central student subscription management API for AU student digests.

Passwordless design: every state-changing action (subscribe, change settings,
unsubscribe) sends a confirmation email to the AU inbox. Clicking the
confirmation link completes the action. The AU email IS the authentication.
"""

from __future__ import annotations

import base64
import html
import json
import os
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler
from typing import Any

import importlib.util
from pathlib import Path

_reg_path = Path(__file__).with_name("_registry.py")
_spec = importlib.util.spec_from_file_location("_registry", _reg_path)
_registry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_registry)

DEFAULT_MAX_PAPERS = _registry.DEFAULT_MAX_PAPERS
build_student_record = _registry.build_student_record
clamp_max_papers = _registry.clamp_max_papers
now_iso = _registry.now_iso
normalise_email = _registry.normalise_email
normalise_package_ids = _registry.normalise_package_ids
package_labels = _registry.package_labels
public_record = _registry.public_record
generate_confirmation_token = _registry.generate_confirmation_token
validate_confirmation_token = _registry.validate_confirmation_token
store_pending_token = _registry.store_pending_token
check_rate_limit = _registry.check_rate_limit
cleanup_expired_tokens = _registry.cleanup_expired_tokens
AU_STUDENT_TRACK_LABELS = _registry.AU_STUDENT_TRACK_LABELS

GITHUB_API = "https://api.github.com"
STORAGE_GITHUB_TOKEN = os.environ.get("STUDENT_STORAGE_GITHUB_TOKEN", "").strip()
STORAGE_REPO = os.environ.get("STUDENT_STORAGE_REPO", "").strip()
STORAGE_PATH = os.environ.get("STUDENT_STORAGE_PATH", "students/subscriptions.json").strip()
STORAGE_BRANCH = os.environ.get("STUDENT_STORAGE_BRANCH", "main").strip()
STUDENT_ADMIN_TOKEN = os.environ.get("STUDENT_ADMIN_TOKEN", "").strip()
STUDENT_TOKEN_SECRET = os.environ.get("STUDENT_TOKEN_SECRET", "").strip()
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()
PUBLIC_STUDENT_MANAGE_URL = os.environ.get(
    "PUBLIC_STUDENT_MANAGE_URL",
    "https://arxiv-digest-relay.vercel.app/api/students",
).strip()

# ─────── Brand constants (synced from brand.py) ──────────────
# Cannot import brand.py because relay deploys from relay/ as root.
_PINE = "#2F4F3E"
_GOLD = "#EBC944"
_ASH_WHITE = "#F6F5F2"
_CHARCOAL = "#1F1F1F"
_WARM_GREY = "#6A6A66"
_ALERT_RED = "#C0392B"


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
            return {"students": {}, "pending_tokens": {}}, None
        raise

    content = base64.b64decode(data["content"]).decode("utf-8")
    registry = json.loads(content) if content.strip() else {}
    if not isinstance(registry, dict):
        registry = {}
    registry.setdefault("students", {})
    registry.setdefault("pending_tokens", {})
    cleanup_expired_tokens(registry["pending_tokens"])
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


def _build_manage_url(email: str) -> str:
    """Return the public manage-page URL for a student subscription."""
    return (
        f"{PUBLIC_STUDENT_MANAGE_URL.rstrip('?')}"
        f"?{urllib.parse.urlencode({'email': email})}"
    )


def _build_confirm_url(token: str) -> str:
    """Return the public confirmation URL for a token."""
    return (
        f"{PUBLIC_STUDENT_MANAGE_URL.rstrip('?')}"
        f"?{urllib.parse.urlencode({'action': 'confirm', 'token': token})}"
    )


# ─────── Email sending ───────────────────────────────────────

def _send_subscribe_confirmation(
    email: str, token: str, package_ids: list[str],
) -> tuple[bool, str | None]:
    """Send a confirmation email for a new or updated subscription."""
    if not SMTP_USER or not SMTP_PASSWORD:
        return False, "confirmation mail is not configured on the relay"

    confirm_url = _build_confirm_url(token)
    package_text = ", ".join(
        package_labels().get(pid, pid) for pid in package_ids
    )
    subject = "Confirm your AU student digest subscription"
    plain_text = (
        f"Confirm your subscription\n\n"
        f"Click the link below to activate your AU student digest:\n"
        f"{confirm_url}\n\n"
        f"Your packages: {package_text}\n\n"
        f"If you didn't request this, you can safely ignore this email.\n"
    )
    html_body = f"""<!doctype html>
<html lang="en">
  <body style="margin:0;padding:0;background:{_ASH_WHITE};font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif">
    <table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto">
      <tr><td style="background:{_PINE};padding:20px 28px">
        <div style="font-family:'DM Serif Display',Georgia,serif;font-size:20px;color:white">AU student digest</div>
      </td></tr>
      <tr><td style="background:white;padding:32px 28px">
        <h1 style="font-family:'DM Serif Display',Georgia,serif;font-size:24px;color:{_CHARCOAL};margin:0 0 16px">Confirm your subscription</h1>
        <p style="font-size:15px;color:{_CHARCOAL};line-height:1.6;margin:0 0 24px">
          Click the button below to activate your weekly arXiv digest.
        </p>
        <a href="{html.escape(confirm_url)}" style="display:inline-block;background:{_PINE};color:white;font-size:15px;font-weight:600;padding:12px 32px;border-radius:8px;text-decoration:none">Confirm subscription</a>
        <div style="margin-top:24px;padding:16px;background:#F8F7F4;border-radius:8px">
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.1em;color:{_WARM_GREY};margin-bottom:8px">YOUR PACKAGES</div>
          <div style="font-size:14px;color:{_CHARCOAL}">{html.escape(package_text)}</div>
        </div>
        <p style="font-size:13px;color:{_WARM_GREY};margin-top:24px;line-height:1.5">
          If you didn't request this, you can safely ignore this email. The link expires in 1 hour.
        </p>
      </td></tr>
    </table>
  </body>
</html>"""

    return _send_email(email, subject, plain_text, html_body)


def _send_unsubscribe_confirmation(
    email: str, token: str,
) -> tuple[bool, str | None]:
    """Send a confirmation email for unsubscribing."""
    if not SMTP_USER or not SMTP_PASSWORD:
        return False, "confirmation mail is not configured on the relay"

    confirm_url = _build_confirm_url(token)
    subject = "Confirm unsubscribe from AU student digest"
    plain_text = (
        f"Confirm unsubscribe\n\n"
        f"Click the link below to unsubscribe from the AU student digest:\n"
        f"{confirm_url}\n\n"
        f"If you didn't request this, you can safely ignore this email.\n"
    )
    html_body = f"""<!doctype html>
<html lang="en">
  <body style="margin:0;padding:0;background:{_ASH_WHITE};font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif">
    <table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto">
      <tr><td style="background:{_PINE};padding:20px 28px">
        <div style="font-family:'DM Serif Display',Georgia,serif;font-size:20px;color:white">AU student digest</div>
      </td></tr>
      <tr><td style="background:white;padding:32px 28px">
        <h1 style="font-family:'DM Serif Display',Georgia,serif;font-size:24px;color:{_CHARCOAL};margin:0 0 16px">Confirm unsubscribe</h1>
        <p style="font-size:15px;color:{_CHARCOAL};line-height:1.6;margin:0 0 24px">
          Click the button below to stop receiving your weekly arXiv digest.
        </p>
        <a href="{html.escape(confirm_url)}" style="display:inline-block;background:{_ALERT_RED};color:white;font-size:15px;font-weight:600;padding:12px 32px;border-radius:8px;text-decoration:none">Confirm unsubscribe</a>
        <p style="font-size:13px;color:{_WARM_GREY};margin-top:24px;line-height:1.5">
          If you didn't request this, you can safely ignore this email. The link expires in 1 hour.
        </p>
      </td></tr>
    </table>
  </body>
</html>"""

    return _send_email(email, subject, plain_text, html_body)


def _send_email(
    to: str, subject: str, plain_text: str, html_body: str,
) -> tuple[bool, str | None]:
    """Send a multipart email via Gmail SMTP."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"arXiv Digest <{SMTP_USER}>"
    msg["To"] = to
    msg.attach(MIMEText(plain_text, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, [to], msg.as_bytes())
        return True, None
    except smtplib.SMTPAuthenticationError:
        return False, "relay SMTP authentication failed"
    except Exception as exc:
        return False, str(exc)


# ─────── API handlers ────────────────────────────────────────

def _handle_request_subscribe(body: dict[str, Any]) -> dict[str, Any]:
    """Validate subscription request and send confirmation email."""
    email = normalise_email(body.get("email", ""))
    package_ids = normalise_package_ids(body.get("package_ids", []))
    max_papers = clamp_max_papers(body.get("max_papers_per_week", DEFAULT_MAX_PAPERS))

    if not STUDENT_TOKEN_SECRET:
        raise RuntimeError("Token secret not configured.")

    registry, sha = _load_registry()
    pending = registry.get("pending_tokens", {})

    check_rate_limit(pending, email, "subscribe")

    payload = {"package_ids": package_ids, "max_papers_per_week": max_papers}
    token = generate_confirmation_token(email, "subscribe", payload, STUDENT_TOKEN_SECRET)
    store_pending_token(pending, email, "subscribe", token)
    registry["pending_tokens"] = pending
    _save_registry(registry, sha, f"Pending subscribe confirmation for {email}")

    sent, err = _send_subscribe_confirmation(email, token, package_ids)

    return {
        "ok": True,
        "confirmation_sent": sent,
        "confirmation_error": err,
    }


def _handle_request_unsubscribe(body: dict[str, Any]) -> dict[str, Any]:
    """Validate unsubscribe request and send confirmation email."""
    email = normalise_email(body.get("email", ""))

    if not STUDENT_TOKEN_SECRET:
        raise RuntimeError("Token secret not configured.")

    registry, sha = _load_registry()
    pending = registry.get("pending_tokens", {})

    check_rate_limit(pending, email, "unsubscribe")

    token = generate_confirmation_token(email, "unsubscribe", {}, STUDENT_TOKEN_SECRET)
    store_pending_token(pending, email, "unsubscribe", token)
    registry["pending_tokens"] = pending
    _save_registry(registry, sha, f"Pending unsubscribe confirmation for {email}")

    sent, err = _send_unsubscribe_confirmation(email, token)

    return {
        "ok": True,
        "confirmation_sent": sent,
        "confirmation_error": err,
    }


def _handle_confirm(token_str: str) -> tuple[str, str]:
    """Validate token and execute the confirmed action.

    Returns (html_page, content_type) for the GET response.
    """
    if not STUDENT_TOKEN_SECRET:
        return _token_error_page("Token verification is not configured."), "text/html"

    try:
        data = validate_confirmation_token(token_str, STUDENT_TOKEN_SECRET)
    except ValueError as exc:
        return _token_error_page(str(exc)), "text/html"

    email = data["email"]
    action = data["action"]
    payload = data.get("payload", {})

    registry, sha = _load_registry()

    if action == "subscribe":
        existing = registry["students"].get(email)
        record = build_student_record(
            email=email,
            package_ids=payload.get("package_ids", []),
            max_papers_per_week=payload.get("max_papers_per_week", DEFAULT_MAX_PAPERS),
            existing=existing,
        )
        registry["students"][email] = record
        # Clean up pending token
        pending = registry.get("pending_tokens", {})
        pending.pop(f"{email}:subscribe", None)
        _save_registry(registry, sha, f"Confirmed subscription for {email}")
        return _subscribe_success_page(public_record(record)), "text/html"

    elif action == "unsubscribe":
        record = registry["students"].get(email)
        if record:
            record = dict(record)
            record["active"] = False
            record["updated_at"] = now_iso()
            registry["students"][email] = record
        pending = registry.get("pending_tokens", {})
        pending.pop(f"{email}:unsubscribe", None)
        _save_registry(registry, sha, f"Confirmed unsubscribe for {email}")
        return _unsubscribe_success_page(), "text/html"

    return _token_error_page("Unknown action."), "text/html"


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
    if action == "request_subscribe":
        return 200, _handle_request_subscribe(body)
    if action == "request_unsubscribe":
        return 200, _handle_request_unsubscribe(body)
    if action == "admin_list":
        return 200, _handle_admin_list(body)
    return 400, {"error": "unknown action"}


# ─────── Landing pages ───────────────────────────────────────

def _subscribe_success_page(subscription: dict[str, Any]) -> str:
    """Confirmation success page after subscribing."""
    package_text = ", ".join(
        package_labels().get(pid, pid) for pid in subscription.get("package_ids", [])
    )
    manage_url = _build_manage_url(subscription["email"])
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Subscribed — AU student digest</title>
</head>
<body style="margin:0;padding:24px;background:{_ASH_WHITE};font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;color:{_CHARCOAL}">
  <div style="max-width:480px;margin:60px auto;text-align:center">
    <div style="width:64px;height:64px;border-radius:50%;background:{_PINE};margin:0 auto 24px;display:flex;align-items:center;justify-content:center">
      <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
    </div>
    <h1 style="font-family:'DM Serif Display',Georgia,serif;font-size:28px;margin:0 0 12px">You're subscribed!</h1>
    <p style="font-size:15px;color:{_WARM_GREY};line-height:1.6;margin:0 0 24px">
      Your first digest arrives next Monday at 07:00 UTC.
    </p>
    <div style="padding:16px;background:white;border-radius:8px;border:1px solid #E5E3DE;margin-bottom:24px;text-align:left">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.1em;color:{_WARM_GREY};margin-bottom:8px">YOUR PACKAGES</div>
      <div style="font-size:14px;color:{_CHARCOAL}">{html.escape(package_text)}</div>
    </div>
    <a href="{html.escape(manage_url)}" style="font-size:14px;color:{_PINE};text-decoration:none">Change settings &rarr;</a>
  </div>
</body>
</html>"""


def _unsubscribe_success_page() -> str:
    """Confirmation success page after unsubscribing."""
    manage_url = PUBLIC_STUDENT_MANAGE_URL
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Unsubscribed — AU student digest</title>
</head>
<body style="margin:0;padding:24px;background:{_ASH_WHITE};font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;color:{_CHARCOAL}">
  <div style="max-width:480px;margin:60px auto;text-align:center">
    <div style="width:64px;height:64px;border-radius:50%;background:{_ALERT_RED};margin:0 auto 24px;display:flex;align-items:center;justify-content:center">
      <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="3" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </div>
    <h1 style="font-family:'DM Serif Display',Georgia,serif;font-size:28px;margin:0 0 12px">You've been unsubscribed</h1>
    <p style="font-size:15px;color:{_WARM_GREY};line-height:1.6;margin:0 0 24px">
      You won't receive any more weekly digests.
    </p>
    <a href="{html.escape(manage_url)}" style="font-size:14px;color:{_PINE};text-decoration:none">Resubscribe &rarr;</a>
  </div>
</body>
</html>"""


def _token_error_page(message: str) -> str:
    """Error page for expired or invalid tokens."""
    manage_url = PUBLIC_STUDENT_MANAGE_URL
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Link expired — AU student digest</title>
</head>
<body style="margin:0;padding:24px;background:{_ASH_WHITE};font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;color:{_CHARCOAL}">
  <div style="max-width:480px;margin:60px auto;text-align:center">
    <div style="font-size:48px;margin-bottom:16px">&#x26A0;&#xFE0F;</div>
    <h1 style="font-family:'DM Serif Display',Georgia,serif;font-size:24px;margin:0 0 12px">Something went wrong</h1>
    <p style="font-size:15px;color:{_WARM_GREY};line-height:1.6;margin:0 0 24px">
      {html.escape(message)}<br>
      Please try again from the settings page.
    </p>
    <a href="{html.escape(manage_url)}" style="display:inline-block;background:{_PINE};color:white;font-size:14px;font-weight:600;padding:10px 24px;border-radius:8px;text-decoration:none">Go to settings &rarr;</a>
  </div>
</body>
</html>"""


# ─────── Settings page ───────────────────────────────────────

def _manage_page(
    email: str,
    mode: str,
    package_ids: list[str] | None = None,
    max_papers_per_week: int = DEFAULT_MAX_PAPERS,
) -> str:
    """Return the passwordless student subscription management page."""
    safe_email = html.escape(email)
    initial_packages = json.dumps(list(package_ids or []))
    initial_max_papers = clamp_max_papers(max_papers_per_week)
    packages_markup = "\n".join(
        f"""
        <label class="package">
          <input type="checkbox" name="package_ids" value="{html.escape(package_id)}">
          <span>{html.escape(label)}</span>
        </label>
        """
        for package_id, label in package_labels().items()
    )
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>AU student digest</title>
    <style>
      :root {{
        --pine: {_PINE};
        --gold: {_GOLD};
        --ash-white: {_ASH_WHITE};
        --charcoal: {_CHARCOAL};
        --warm-grey: {_WARM_GREY};
        --border: #D8D6D0;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100vh;
        background: var(--ash-white);
        color: var(--charcoal);
        font-family: "IBM Plex Sans", Helvetica, Arial, sans-serif;
        padding: 24px;
      }}
      main {{
        width: min(100%, 520px);
        margin: 0 auto;
        background: white;
        border-bottom: 3px solid var(--pine);
        border-radius: 12px;
        padding: 36px 32px 32px;
        box-shadow: 0 8px 32px rgba(0,0,0,0.06);
      }}
      h1 {{
        font-family: "DM Serif Display", Georgia, serif;
        margin: 0 0 4px;
        font-size: 28px;
        color: var(--charcoal);
        line-height: 1.1;
      }}
      .subtitle {{
        color: var(--warm-grey);
        font-size: 14px;
        margin: 0 0 28px;
        line-height: 1.5;
      }}
      .section-label {{
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.12em;
        color: var(--warm-grey);
        font-weight: 600;
        margin: 0 0 12px;
      }}
      .divider {{
        border: none;
        border-top: 1px solid var(--border);
        margin: 24px 0;
      }}
      .field label {{
        display: block;
        font-size: 13px;
        color: var(--warm-grey);
        margin-bottom: 4px;
        font-weight: 500;
      }}
      .email-row {{
        display: flex;
        align-items: center;
        gap: 8px;
      }}
      .email-affix {{
        font-size: 15px;
        color: var(--warm-grey);
        font-family: "IBM Plex Mono", monospace;
        white-space: nowrap;
        user-select: none;
      }}
      .email-row input {{
        border: 1px solid var(--border);
        border-radius: 8px;
        width: 90px;
        padding: 10px 12px;
        text-align: center;
        font-family: "IBM Plex Mono", monospace;
        font-size: 15px;
        outline: none;
        background: #F8F7F4;
      }}
      .email-row input:focus {{
        border-color: var(--pine);
        box-shadow: 0 0 0 2px rgba(47,79,62,0.12);
      }}
      .packages {{
        display: flex;
        flex-direction: column;
        gap: 6px;
        margin-bottom: 20px;
      }}
      .package {{
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 10px 12px;
        border: 1px solid var(--border);
        border-radius: 8px;
        background: white;
        cursor: pointer;
        transition: border-color 0.15s, background 0.15s;
        font-size: 14px;
      }}
      .package:has(input:checked) {{
        border-color: var(--pine);
        background: rgba(47,79,62,0.04);
      }}
      .package input[type="checkbox"] {{
        accent-color: var(--pine);
        width: 16px;
        height: 16px;
      }}
      .stepper-row {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 4px;
      }}
      .stepper-row > label {{
        font-size: 14px;
        color: var(--charcoal);
        font-weight: 400;
      }}
      .stepper {{
        display: flex;
        align-items: center;
        gap: 0;
        border: 1px solid var(--border);
        border-radius: 8px;
        overflow: hidden;
      }}
      .stepper button {{
        width: 40px;
        height: 40px;
        border: none;
        border-right: 1px solid var(--border);
        background: white;
        font-size: 18px;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        color: var(--charcoal);
      }}
      .stepper button:last-child {{
        border-right: none;
        border-left: 1px solid var(--border);
      }}
      .stepper button:hover {{
        background: var(--ash-white);
      }}
      .stepper-value {{
        font-family: "DM Serif Display", Georgia, serif;
        font-size: 20px;
        min-width: 48px;
        text-align: center;
      }}
      button.primary {{
        border: 0;
        border-radius: 8px;
        padding: 14px 20px;
        background: var(--pine);
        color: white;
        cursor: pointer;
        font-weight: 600;
        font-size: 15px;
        width: 100%;
        transition: opacity 0.15s;
      }}
      button.primary:hover {{
        opacity: 0.9;
      }}
      .confirm-note {{
        font-size: 13px;
        color: var(--warm-grey);
        text-align: center;
        margin-top: 12px;
        line-height: 1.4;
      }}
      .unsub-section {{
        text-align: center;
        margin-top: 20px;
      }}
      .unsub-link {{
        background: none;
        border: 0;
        padding: 0;
        color: var(--warm-grey);
        font-size: 13px;
        cursor: pointer;
        text-decoration: underline;
        text-underline-offset: 2px;
      }}
      .unsub-link:hover {{
        color: {_ALERT_RED};
      }}
      .unsub-hint {{
        font-size: 12px;
        color: var(--warm-grey);
        margin-top: 4px;
        opacity: 0.7;
      }}
      .status {{
        margin-top: 16px;
        padding: 12px 14px;
        border-radius: 8px;
        background: rgba(47,79,62,0.06);
        color: var(--pine);
        font-size: 14px;
        line-height: 1.4;
        text-align: center;
      }}
      .status:empty {{
        display: none;
      }}
      .status.error {{
        background: rgba(192,57,43,0.06);
        color: {_ALERT_RED};
      }}
    </style>
  </head>
  <body>
    <main>
      <h1>AU student digest</h1>
      <p class="subtitle">Draws from the daily arXiv archive, scored for your interests.</p>

      <!-- AU student ID -->
      <div class="field" style="margin-bottom:24px">
        <label for="email-digits">AU student ID</label>
        <div class="email-row">
          <span class="email-affix">au</span>
          <input id="email-digits" type="text" inputmode="numeric" pattern="\\d{{6}}" maxlength="6" value="{safe_email.replace('au','').replace('@uni.au.dk','')}" placeholder="612345">
          <span class="email-affix">@uni.au.dk</span>
        </div>
      </div>

      <hr class="divider">

      <!-- Packages -->
      <div class="section-label">PICK YOUR PACKAGES</div>
      <div class="packages">
        {packages_markup}
      </div>

      <!-- Max papers stepper -->
      <div class="stepper-row">
        <label>Max papers per week</label>
        <div class="stepper">
          <button type="button" onclick="adjustMax(-1)">&minus;</button>
          <span id="max-display" class="stepper-value">{initial_max_papers}</span>
          <button type="button" onclick="adjustMax(1)">+</button>
        </div>
        <input id="max_papers" type="hidden" value="{initial_max_papers}">
      </div>

      <!-- Subscribe button -->
      <button class="primary" type="button" onclick="saveSubscription()">Subscribe</button>
      <div class="confirm-note">We'll send a confirmation link to your AU email.</div>

      <div id="status" class="status"></div>

      <!-- Unsubscribe -->
      <div class="unsub-section">
        <button class="unsub-link" type="button" onclick="handleUnsubscribe()">Unsubscribe</button>
        <div class="unsub-hint">Fill in your AU ID above first.</div>
      </div>
    </main>
    <script>
      const initialPackages = {initial_packages};
      const initialMaxPapers = {initial_max_papers};
      const statusEl = document.getElementById("status");
      let maxPapers = initialMaxPapers;

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

      function adjustMax(delta) {{
        maxPapers = Math.min(20, Math.max(1, maxPapers + delta));
        document.getElementById("max-display").textContent = maxPapers;
        document.getElementById("max_papers").value = maxPapers;
      }}

      function setStatus(message, isError) {{
        statusEl.textContent = message;
        statusEl.className = "status" + (isError ? " error" : "");
        statusEl.style.display = message ? "block" : "none";
      }}

      function getEmail() {{
        const digits = document.getElementById("email-digits").value.trim();
        if (!/^\\d{{6}}$/.test(digits)) {{
          throw new Error("Enter your 6-digit AU student ID.");
        }}
        return "au" + digits + "@uni.au.dk";
      }}

      async function saveSubscription() {{
        setStatus("", false);
        try {{
          const email = getEmail();
          const packages = selectedPackages();
          if (packages.length === 0) {{
            setStatus("Pick at least one package.", true);
            return;
          }}
          setStatus("Sending confirmation...", false);
          const response = await fetch(window.location.pathname, {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{
              action: "request_subscribe",
              email: email,
              package_ids: packages,
              max_papers_per_week: maxPapers,
            }}),
          }});
          const data = await response.json();
          if (!response.ok || !data.ok) {{
            setStatus(data.error || "Request failed", true);
            return;
          }}
          if (data.confirmation_sent) {{
            setStatus("Check your AU email for a confirmation link.");
          }} else {{
            setStatus("Could not send confirmation: " + (data.confirmation_error || "unknown error"), true);
          }}
        }} catch (error) {{
          setStatus(error.message, true);
        }}
      }}

      async function handleUnsubscribe() {{
        setStatus("", false);
        try {{
          const email = getEmail();
          setStatus("Sending confirmation...", false);
          const response = await fetch(window.location.pathname, {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{
              action: "request_unsubscribe",
              email: email,
            }}),
          }});
          const data = await response.json();
          if (!response.ok || !data.ok) {{
            setStatus(data.error || "Request failed", true);
            return;
          }}
          if (data.confirmation_sent) {{
            setStatus("Check your AU email to confirm unsubscribe.");
          }} else {{
            setStatus("Could not send confirmation: " + (data.confirmation_error || "unknown error"), true);
          }}
        }} catch (error) {{
          setStatus(error.message, true);
        }}
      }}

      // Initialise
      setPackages(initialPackages);
    </script>
  </body>
</html>"""


class handler(BaseHTTPRequestHandler):
    """Vercel Python serverless handler."""

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        # Token confirmation flow
        action = query.get("action", [""])[0]
        token = query.get("token", [""])[0]
        if action == "confirm" and token:
            page, content_type = _handle_confirm(token)
            payload = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        # Settings page
        email = query.get("email", [""])[0]
        mode = query.get("mode", [""])[0]
        raw_packages = query.get("packages", [""])[0]
        package_ids: list[str]
        if raw_packages.strip():
            try:
                package_ids = normalise_package_ids(raw_packages.split(","))
            except ValueError:
                package_ids = []
        else:
            package_ids = []
        max_papers = clamp_max_papers(query.get("max_papers", [DEFAULT_MAX_PAPERS])[0])
        page = _manage_page(email, mode, package_ids, max_papers)
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
