"""Inlined student registry helpers for the Vercel relay runtime.

The relay deploys from relay/ as root, so it cannot import from the
repo-root student_registry.py or setup/data.py. This module duplicates
the minimal subset needed by the API endpoints.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
_AU_EMAIL_RE = re.compile(r"^au\d{6}@uni\.au\.dk$")

# ─────── Package definitions (synced from setup/data.py) ──────

AU_STUDENT_TRACK_LABELS = {
    "au_astronomy": "AU Astronomy",
    "stars": "Stars",
    "galaxies": "Galaxies",
    "cosmology": "Cosmology",
    "exoplanets": "Planets & exoplanets",
    "high_energy": "High-energy astrophysics",
    "instrumentation": "Instrumentation",
    "solar_helio": "Solar & heliophysics",
    "methods_ml": "Methods & machine learning",
}

DEFAULT_MAX_PAPERS = 6
MIN_MAX_PAPERS = 1
MAX_MAX_PAPERS = 20
AVAILABLE_STUDENT_PACKAGES = [
    "exoplanets", "stars", "galaxies", "cosmology",
    "high_energy", "instrumentation", "solar_helio", "methods_ml",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalise_email(email: str) -> str:
    normalised = " ".join(str(email).split()).strip().lower()
    if normalised and not _EMAIL_RE.match(normalised):
        raise ValueError(f"Invalid email address: {normalised!r}")
    if normalised and not _AU_EMAIL_RE.match(normalised):
        raise ValueError("Only AU student emails are accepted (auXXXXXX@uni.au.dk).")
    return normalised


def package_labels() -> dict[str, str]:
    return {key: AU_STUDENT_TRACK_LABELS[key] for key in AVAILABLE_STUDENT_PACKAGES}


def normalise_package_ids(package_ids: Any) -> list[str]:
    cleaned: list[str] = []
    for package_id in package_ids or []:
        key = str(package_id).strip()
        if key in AVAILABLE_STUDENT_PACKAGES and key not in cleaned:
            cleaned.append(key)
    if not cleaned:
        raise ValueError("Pick at least one student package.")
    return cleaned


def clamp_max_papers(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_MAX_PAPERS
    return max(MIN_MAX_PAPERS, min(MAX_MAX_PAPERS, parsed))



def public_record(record: dict[str, Any]) -> dict[str, Any]:
    normalized = normalise_public_subscription(record)
    return {
        "email": normalized["email"],
        "package_ids": normalized["package_ids"],
        "max_papers_per_week": normalized["max_papers_per_week"],
        "active": normalized["active"],
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }


def normalise_public_subscription(record: dict[str, Any]) -> dict[str, Any]:
    email = normalise_email(record.get("email", ""))
    if not email:
        raise ValueError("Subscription record is missing an email.")
    return {
        "email": email,
        "package_ids": normalise_package_ids(record.get("package_ids", [])),
        "max_papers_per_week": clamp_max_papers(record.get("max_papers_per_week")),
        "active": bool(record.get("active", True)),
    }


def build_student_record(
    *,
    email: str,
    package_ids: Any,
    max_papers_per_week: Any,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or update a student subscription record (passwordless)."""
    clean_email = normalise_email(email)
    if not clean_email:
        raise ValueError("Email is required.")
    packages = normalise_package_ids(package_ids)
    max_papers = clamp_max_papers(max_papers_per_week)
    timestamp = now_iso()
    created_at = (existing.get("created_at") or timestamp) if existing else timestamp

    return {
        "email": clean_email,
        "package_ids": packages,
        "max_papers_per_week": max_papers,
        "active": True,
        "created_at": created_at,
        "updated_at": timestamp,
    }


# ─────── Confirmation token system ───────────────────────────

_TOKEN_DEFAULT_TTL = 3600  # 1 hour
_RATE_LIMIT_SECONDS = 15 * 60  # 15 minutes


def generate_confirmation_token(
    email: str,
    action: str,
    payload: dict[str, Any],
    secret: str,
    *,
    ttl_seconds: int = _TOKEN_DEFAULT_TTL,
) -> str:
    """Create an HMAC-signed URL-safe confirmation token (stdlib only)."""
    data = {
        "email": email,
        "action": action,
        "payload": payload,
        "expires_at": time.time() + ttl_seconds,
        "nonce": os.urandom(8).hex(),
    }
    data_bytes = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    data_b64 = base64.urlsafe_b64encode(data_bytes).decode("ascii")
    sig = hmac.new(secret.encode("utf-8"), data_bytes, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode("ascii")
    return f"{data_b64}.{sig_b64}"


def validate_confirmation_token(token: str, secret: str) -> dict[str, Any]:
    """Decode and verify an HMAC-signed confirmation token.

    Raises ValueError on invalid, tampered, or expired tokens.
    """
    try:
        parts = token.split(".", 1)
        if len(parts) != 2:
            raise ValueError("Invalid token format.")
        data_b64, sig_b64 = parts
        data_bytes = base64.urlsafe_b64decode(data_b64)
        expected_sig = hmac.new(
            secret.encode("utf-8"), data_bytes, hashlib.sha256,
        ).digest()
        actual_sig = base64.urlsafe_b64decode(sig_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            raise ValueError("Invalid token signature.")
        data = json.loads(data_bytes)
    except (ValueError, json.JSONDecodeError) as exc:
        if "expired" in str(exc).lower() or "invalid" in str(exc).lower():
            raise
        raise ValueError("Invalid token.") from exc

    if data.get("expires_at", 0) < time.time():
        raise ValueError("Token has expired.")
    return data


def store_pending_token(
    pending: dict[str, Any], email: str, action: str, token: str,
) -> None:
    """Record a pending token for rate-limit tracking."""
    key = f"{email}:{action}"
    # Decode to get expiry
    try:
        data = json.loads(base64.urlsafe_b64decode(token.split(".")[0]))
        expires_at = data.get("expires_at", time.time() + _TOKEN_DEFAULT_TTL)
    except Exception:
        expires_at = time.time() + _TOKEN_DEFAULT_TTL
    pending[key] = {
        "token": token,
        "created_at": time.time(),
        "expires_at": expires_at,
    }


def check_rate_limit(
    pending: dict[str, Any], email: str, action: str,
) -> None:
    """Raise ValueError if a recent confirmation was already sent."""
    key = f"{email}:{action}"
    entry = pending.get(key)
    if entry and (time.time() - entry["created_at"]) < _RATE_LIMIT_SECONDS:
        raise ValueError(
            "A recent confirmation was already sent. "
            "Please check your email or wait 15 minutes."
        )


def cleanup_expired_tokens(pending: dict[str, Any]) -> None:
    """Remove expired entries from the pending tokens dict."""
    now = time.time()
    expired_keys = [
        key for key, entry in pending.items()
        if entry.get("expires_at", 0) < now
    ]
    for key in expired_keys:
        del pending[key]
