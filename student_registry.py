"""Shared helpers for central student subscriptions."""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timezone
from typing import Any

from setup.data import AU_STUDENT_TRACK_LABELS

DEFAULT_MAX_PAPERS = 6
MIN_MAX_PAPERS = 1
MAX_MAX_PAPERS = 20
AVAILABLE_STUDENT_PACKAGES = [
    track_id
    for track_id in [
        "stars", "exoplanets", "galaxies", "cosmology",
        "high_energy", "instrumentation", "solar_helio", "methods_ml",
    ]
    if track_id in AU_STUDENT_TRACK_LABELS
]


def now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalise_email(email: str) -> str:
    """Return a canonical lower-cased email string."""
    return " ".join(str(email).split()).strip().lower()


def package_labels() -> dict[str, str]:
    """Return the supported student package label map."""
    return {key: AU_STUDENT_TRACK_LABELS[key] for key in AVAILABLE_STUDENT_PACKAGES}


def normalise_package_ids(package_ids: Any) -> list[str]:
    """Validate and de-duplicate selected package ids."""
    cleaned: list[str] = []
    for package_id in package_ids or []:
        key = str(package_id).strip()
        if key in AVAILABLE_STUDENT_PACKAGES and key not in cleaned:
            cleaned.append(key)
    if not cleaned:
        raise ValueError("Pick at least one student package.")
    return cleaned


def clamp_max_papers(value: Any) -> int:
    """Clamp max-papers settings into the supported student range."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_MAX_PAPERS
    return max(MIN_MAX_PAPERS, min(MAX_MAX_PAPERS, parsed))


def _preferred_password_scheme() -> str:
    """Return the strongest password-hash scheme available in this runtime."""
    return "scrypt" if hasattr(hashlib, "scrypt") else "pbkdf2_sha256"


def hash_password(password: str, *, salt_hex: str | None = None) -> tuple[str, str]:
    """Hash a password with scrypt, returning (salt_hex, hash_hex)."""
    if not password:
        raise ValueError("Password is required.")
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    scheme = _preferred_password_scheme()
    if scheme == "scrypt":
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    else:
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return salt.hex(), f"{scheme}${digest.hex()}"


def verify_password(password: str, salt_hex: str, digest_hex: str) -> bool:
    """Return True when the password matches the stored hash."""
    if not password or not salt_hex or not digest_hex:
        return False
    scheme, _, digest_value = digest_hex.partition("$")
    if not digest_value:
        scheme = _preferred_password_scheme()
        digest_value = digest_hex

    salt = bytes.fromhex(salt_hex)
    if scheme == "scrypt" and hasattr(hashlib, "scrypt"):
        candidate = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1).hex()
    else:
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000).hex()
        scheme = "pbkdf2_sha256"
    return hmac.compare_digest(f"{scheme}${candidate}", f"{scheme}${digest_value}")


def public_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return the non-sensitive fields of a student record."""
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
    """Return a validated public subscription record."""
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
    password: str,
    new_password: str | None = None,
    package_ids: Any,
    max_papers_per_week: Any,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or update a student subscription record."""
    clean_email = normalise_email(email)
    if not clean_email:
        raise ValueError("Email is required.")
    packages = normalise_package_ids(package_ids)
    max_papers = clamp_max_papers(max_papers_per_week)
    replacement_password = str(new_password or "").strip()
    timestamp = now_iso()

    if existing:
        salt_hex = str(existing.get("password_salt", "")).strip()
        digest_hex = str(existing.get("password_hash", "")).strip()
        if not verify_password(password, salt_hex, digest_hex):
            raise PermissionError("Incorrect password.")
        if replacement_password:
            password_salt, password_hash = hash_password(replacement_password)
        else:
            password_salt = salt_hex
            password_hash = digest_hex
        created_at = existing.get("created_at") or timestamp
    else:
        password_salt, password_hash = hash_password(password)
        created_at = timestamp

    return {
        "email": clean_email,
        "package_ids": packages,
        "max_papers_per_week": max_papers,
        "active": True,
        "password_salt": password_salt,
        "password_hash": password_hash,
        "created_at": created_at,
        "updated_at": timestamp,
    }
