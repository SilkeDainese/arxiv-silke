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
_SETTINGS_TOKEN_TTL = _registry._SETTINGS_TOKEN_TTL

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
_CREAM = "#F5F3EF"        # warm cream background
_WARM_WHITE = "#FFFDF8"   # header text
_FOOTER_BG = "#F0EDE6"    # footer background
_SOFT_GREY = "#BBB"       # soft unsubscribe link
_TERRACOTTA = "#9E5544"   # unsubscribe/danger button
_GREEN_HAND = "#2C5530"   # Kalam handscribble


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
        f"Your categories: {package_text}\n\n"
        f"If you didn't request this, you can safely ignore this email.\n"
    )
    html_body = f"""<!doctype html>
<html lang="en">
  <body style="margin:0;padding:0;background:{_CREAM};font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif">
    <table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto">
      <tr><td style="background:{_PINE};padding:14px 28px">
        <div style="font-family:'DM Serif Display',Georgia,serif;font-size:18px;color:{_WARM_WHITE}">AU student digest</div>
      </td></tr>
      <tr><td style="background:white;padding:32px 28px">
        <h1 style="font-family:'DM Serif Display',Georgia,serif;font-size:24px;color:{_CHARCOAL};margin:0 0 16px">Confirm your subscription</h1>
        <p style="font-size:15px;color:{_CHARCOAL};line-height:1.6;margin:0 0 24px">
          Click the button below to activate your weekly arXiv digest.
        </p>
        <a href="{html.escape(confirm_url)}" style="display:inline-block;background:{_PINE};color:white;font-size:15px;font-weight:600;padding:12px 32px;border-radius:8px;text-decoration:none">Confirm subscription</a>
        <div style="margin-top:24px;padding:16px;background:#F8F7F4;border-radius:8px">
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.1em;color:{_WARM_GREY};margin-bottom:8px">YOUR CATEGORIES</div>
          <div style="font-size:14px;color:{_CHARCOAL}">{html.escape(package_text)}</div>
        </div>
        <p style="font-size:13px;color:{_WARM_GREY};margin-top:24px;line-height:1.5">
          If you didn't request this, you can safely ignore this email. The link expires in 1 hour.
        </p>
        <p style="font-size:13px;color:#999;margin-top:12px;line-height:1.5">
          You can change settings or unsubscribe anytime from your digest email.
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
  <body style="margin:0;padding:0;background:{_CREAM};font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif">
    <table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto">
      <tr><td style="background:{_PINE};padding:14px 28px">
        <div style="font-family:'DM Serif Display',Georgia,serif;font-size:18px;color:{_WARM_WHITE}">AU student digest</div>
      </td></tr>
      <tr><td style="background:white;padding:32px 28px">
        <h1 style="font-family:'DM Serif Display',Georgia,serif;font-size:24px;color:{_CHARCOAL};margin:0 0 16px">Confirm unsubscribe</h1>
        <p style="font-size:15px;color:{_CHARCOAL};line-height:1.6;margin:0 0 24px">
          Click the button below to stop receiving your weekly arXiv digest.
        </p>
        <a href="{html.escape(confirm_url)}" style="display:inline-block;background:{_TERRACOTTA};color:white;font-size:15px;font-weight:600;padding:12px 32px;border-radius:8px;text-decoration:none">Confirm unsubscribe</a>
        <p style="font-size:13px;color:{_WARM_GREY};margin-top:24px;line-height:1.5">
          If you didn't request this, you can safely ignore this email. The link expires in 1 hour.
        </p>
        <p style="font-size:13px;color:#999;margin-top:12px;line-height:1.5">
          You can change settings or unsubscribe anytime from your digest email.
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


def _handle_settings_get(token_str: str) -> tuple[str, str]:
    """Validate a change_settings token and render the settings page.

    The token proves identity (embedded in the digest email footer),
    so the email field is readonly and pre-filled.
    """
    if not STUDENT_TOKEN_SECRET:
        return _token_error_page("Token verification is not configured."), "text/html"

    try:
        data = validate_confirmation_token(token_str, STUDENT_TOKEN_SECRET)
    except ValueError as exc:
        return _token_error_page(str(exc)), "text/html"

    if data.get("action") != "change_settings":
        return _token_error_page("Invalid token type."), "text/html"

    email = data["email"]
    registry, _ = _load_registry()
    record = registry["students"].get(email, {})
    current_packages = record.get("package_ids", [])
    current_max = clamp_max_papers(record.get("max_papers_per_week", DEFAULT_MAX_PAPERS))

    return _manage_page(
        email, "settings", current_packages, current_max,
        settings_token=token_str,
    ), "text/html"


def _handle_settings_post(
    token_str: str,
    package_ids: list[str],
    max_papers_per_week: int,
) -> tuple[str, str]:
    """Apply settings changes directly (no confirmation email needed).

    The token already proves the student clicked from their own inbox.
    """
    if not STUDENT_TOKEN_SECRET:
        return _token_error_page("Token verification is not configured."), "text/html"

    try:
        data = validate_confirmation_token(token_str, STUDENT_TOKEN_SECRET)
    except ValueError as exc:
        return _token_error_page(str(exc)), "text/html"

    if data.get("action") != "change_settings":
        return _token_error_page("Invalid token type."), "text/html"

    email = data["email"]
    registry, sha = _load_registry()
    existing = registry["students"].get(email)
    if not existing:
        return _token_error_page("No subscription found for this email."), "text/html"

    record = build_student_record(
        email=email,
        package_ids=package_ids,
        max_papers_per_week=max_papers_per_week,
        existing=existing,
    )
    registry["students"][email] = record
    _save_registry(registry, sha, f"Settings updated for {email}")

    return _settings_updated_page(public_record(record), token_str), "text/html"


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


def _handle_mark_welcome_sent(body: dict[str, Any]) -> dict[str, Any]:
    """Mark a student's welcome email as sent."""
    _require_admin_token(str(body.get("admin_token", "")))
    email = normalise_email(body.get("email", ""))
    registry, sha = _load_registry()
    record = registry["students"].get(email)
    if not record:
        raise FileNotFoundError(f"No subscription found for {email}")
    record["welcome_sent"] = True
    record["updated_at"] = now_iso()
    _save_registry(registry, sha, f"Marked welcome sent for {email}")
    return {"ok": True}


def _handle_admin_stats(body: dict[str, Any]) -> dict[str, Any]:
    """Return aggregate statistics about student subscriptions."""
    _require_admin_token(str(body.get("admin_token", "")))
    registry, _ = _load_registry()
    total_active = 0
    total_inactive = 0
    welcome_pending = 0
    category_dist: dict[str, int] = {}
    max_papers_dist: dict[str, int] = {}
    for record in registry["students"].values():
        if record.get("active", True):
            total_active += 1
            if not record.get("welcome_sent", False):
                welcome_pending += 1
            for pkg in record.get("package_ids", []):
                category_dist[pkg] = category_dist.get(pkg, 0) + 1
            mp_key = str(record.get("max_papers_per_week", DEFAULT_MAX_PAPERS))
            max_papers_dist[mp_key] = max_papers_dist.get(mp_key, 0) + 1
        else:
            total_inactive += 1
    return {
        "ok": True,
        "total_active": total_active,
        "total_inactive": total_inactive,
        "welcome_pending": welcome_pending,
        "category_distribution": category_dist,
        "max_papers_distribution": max_papers_dist,
    }


def _dispatch(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    action = str(body.get("action", "")).strip().lower()
    if action == "request_subscribe":
        return 200, _handle_request_subscribe(body)
    if action == "request_unsubscribe":
        return 200, _handle_request_unsubscribe(body)
    if action == "admin_list":
        return 200, _handle_admin_list(body)
    if action == "mark_welcome_sent":
        return 200, _handle_mark_welcome_sent(body)
    if action == "admin_stats":
        return 200, _handle_admin_stats(body)
    return 400, {"ok": False, "error": "unknown action"}


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
<body style="margin:0;padding:24px;background:{_CREAM};font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;color:{_CHARCOAL}">
  <div style="max-width:480px;margin:60px auto;text-align:center">
    <div style="width:64px;height:64px;border-radius:50%;background:{_PINE};margin:0 auto 24px;display:flex;align-items:center;justify-content:center">
      <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
    </div>
    <h1 style="font-family:'DM Serif Display',Georgia,serif;font-size:28px;margin:0 0 12px">You're subscribed!</h1>
    <p style="font-size:15px;color:{_WARM_GREY};line-height:1.6;margin:0 0 24px">
      Your first digest arrives next Monday at 07:00 UTC.
    </p>
    <div style="padding:16px;background:white;border-radius:8px;border:1px solid #E5E3DE;margin-bottom:24px;text-align:left">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.1em;color:{_WARM_GREY};margin-bottom:8px">YOUR CATEGORIES</div>
      <div style="font-size:14px;color:{_CHARCOAL}">{html.escape(package_text)}</div>
    </div>
    <a href="{html.escape(manage_url)}" style="font-size:14px;color:{_PINE};text-decoration:none">Want to change something? Update your settings. &rarr;</a>
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
<body style="margin:0;padding:24px;background:{_CREAM};font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;color:{_CHARCOAL}">
  <div style="max-width:480px;margin:60px auto;text-align:center">
    <div style="width:64px;height:64px;border-radius:50%;background:{_TERRACOTTA};margin:0 auto 24px;display:flex;align-items:center;justify-content:center">
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


def _settings_updated_page(subscription: dict[str, Any], settings_token: str) -> str:
    """Success page after applying settings changes."""
    package_text = ", ".join(
        package_labels().get(pid, pid) for pid in subscription.get("package_ids", [])
    )
    email = subscription["email"]
    settings_url = (
        f"{PUBLIC_STUDENT_MANAGE_URL.rstrip('?')}"
        f"?{urllib.parse.urlencode({'action': 'settings', 'token': settings_token})}"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Settings updated — AU student digest</title>
</head>
<body style="margin:0;padding:24px;background:{_CREAM};font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;color:{_CHARCOAL}">
  <div style="max-width:480px;margin:60px auto;text-align:center">
    <div style="width:64px;height:64px;border-radius:50%;background:{_PINE};margin:0 auto 24px;display:flex;align-items:center;justify-content:center">
      <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
    </div>
    <h1 style="font-family:'DM Serif Display',Georgia,serif;font-size:28px;margin:0 0 12px">Settings updated!</h1>
    <p style="font-size:15px;color:{_WARM_GREY};line-height:1.6;margin:0 0 24px">
      Your digest at {html.escape(email)} has been updated. Changes take effect with your next email.
    </p>
    <div style="padding:16px;background:white;border-radius:8px;border:1px solid #E5E3DE;margin-bottom:24px;text-align:left">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.1em;color:{_WARM_GREY};margin-bottom:8px">YOUR CATEGORIES</div>
      <div style="font-size:14px;color:{_CHARCOAL}">{html.escape(package_text)}</div>
    </div>
    <a href="{html.escape(settings_url)}" style="font-size:14px;color:{_PINE};text-decoration:none">Update your settings again. &rarr;</a>
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
<body style="margin:0;padding:24px;background:{_CREAM};font-family:'IBM Plex Sans',Helvetica,Arial,sans-serif;color:{_CHARCOAL}">
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
    *,
    settings_token: str = "",
) -> str:
    """Return the passwordless student subscription management page."""
    is_settings = mode == "settings" and settings_token
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
        --cream: {_CREAM};
        --charcoal: {_CHARCOAL};
        --warm-grey: {_WARM_GREY};
        --border: #D8D6D0;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100vh;
        background: var(--cream);
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
      #email-input:focus {{
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

      <!-- Email -->
      <div class="field" style="margin-bottom:24px">
        <label for="email-input">Email</label>
        <input id="email-input" type="email" value="{safe_email}" placeholder="you@example.com"
          {"readonly" if is_settings else ""}
          style="width:100%;box-sizing:border-box;border:1px solid var(--border);border-radius:8px;padding:10px 12px;
          font-family:'IBM Plex Sans',sans-serif;font-size:14px;background:{"#ECEAE5" if is_settings else "#F8F7F4"};outline:none">
      </div>

      <hr class="divider">

      <!-- Packages -->
      <div class="section-label">PICK YOUR CATEGORIES</div>
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

      <!-- Action button -->
      <button class="primary" type="button" onclick="saveSubscription()">{"Update settings" if is_settings else "Subscribe"}</button>
      <div class="confirm-note">{"Changes take effect with your next digest." if is_settings else "We'll send a confirmation link to your email."}</div>
      {"" if is_settings else '<div style="font-size:13px;color:#999;text-align:center;margin-top:8px;line-height:1.4">You can change settings or unsubscribe anytime from your digest email.</div>'}

      <div id="status" class="status"></div>
      {f'<div style="font-size:13px;text-align:center;margin-top:16px"><a href="{html.escape(_build_manage_url(email))}&amp;mode=unsubscribe" style="color:{_SOFT_GREY};text-decoration:none">Unsubscribe</a></div>' if is_settings else ""}
      <input type="hidden" id="settings-token" value="{html.escape(settings_token)}">
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
        const email = document.getElementById("email-input").value.trim().toLowerCase();
        if (!email || !/^[^\\s@]+@[^\\s@]+\\.[^\\s@]{{2,}}$/.test(email)) {{
          throw new Error("Enter a valid email address.");
        }}
        return email;
      }}

      const settingsToken = document.getElementById("settings-token").value;
      const isSettings = settingsToken !== "";

      async function saveSubscription() {{
        setStatus("", false);
        try {{
          const email = getEmail();
          const packages = selectedPackages();
          if (packages.length === 0) {{
            setStatus("Pick at least one category.", true);
            return;
          }}
          if (isSettings) {{
            setStatus("Saving...", false);
            const params = new URLSearchParams({{
              action: "update_settings",
              token: settingsToken,
            }});
            packages.forEach(p => params.append("package_ids", p));
            params.set("max_papers", String(maxPapers));
            const response = await fetch(window.location.pathname + "?" + params.toString(), {{
              method: "POST",
              headers: {{ "Content-Type": "application/x-www-form-urlencoded" }},
            }});
            if (response.ok) {{
              document.open();
              document.write(await response.text());
              document.close();
            }} else {{
              setStatus("Update failed. Please try again.", true);
            }}
          }} else {{
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
              setStatus("Check your email for a confirmation link.");
            }} else {{
              setStatus("Could not send confirmation: " + (data.confirmation_error || "unknown error"), true);
            }}
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

        # Token-authenticated settings flow
        if action == "settings" and token:
            page, content_type = _handle_settings_get(token)
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
        # Settings update via form POST (token-authenticated)
        path = getattr(self, "path", "")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
        post_action = query.get("action", [""])[0]
        post_token = query.get("token", [""])[0]
        if post_action == "update_settings" and post_token:
            pkg_ids = query.get("package_ids", [])
            max_p = clamp_max_papers(query.get("max_papers", [DEFAULT_MAX_PAPERS])[0])
            try:
                page, content_type = _handle_settings_post(post_token, pkg_ids, max_p)
                payload = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", f"{content_type}; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception:
                self._respond(400, {"ok": False, "error": "settings update failed"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, ValueError):
            self._respond(400, {"ok": False, "error": "invalid JSON"})
            return

        try:
            status, payload = _dispatch(body)
            self._respond(status, payload)
        except PermissionError as exc:
            self._respond(403, {"ok": False, "error": str(exc)})
        except FileNotFoundError as exc:
            self._respond(404, {"ok": False, "error": str(exc)})
        except ValueError as exc:
            self._respond(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            import sys
            import traceback
            print(f"[relay/students] Unhandled error: {traceback.format_exc()}", file=sys.stderr)
            self._respond(500, {"ok": False, "error": "internal server error"})

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
