#!/usr/bin/env python3
"""Terminal manager for central AU student subscriptions."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from setup.data import AU_STUDENT_TRACK_LABELS
from student_registry import AVAILABLE_STUDENT_PACKAGES, DEFAULT_MAX_PAPERS

DEFAULT_REGISTRY_URL = "https://arxiv-digest-relay.vercel.app/api/students"
TRACK_IDS = list(AVAILABLE_STUDENT_PACKAGES)


def prompt(text: str, *, default: str | None = None, required: bool = True) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        value = input(f"{text}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        if not required:
            return ""
        print("Please enter a value.")


def prompt_secret(text: str) -> str:
    while True:
        value = getpass.getpass(f"{text}: ").strip()
        if value:
            return value
        print("Please enter a value.")


def prompt_optional_secret(text: str) -> str:
    return getpass.getpass(f"{text}: ").strip()


def prompt_yes_no(text: str, *, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        value = input(f"{text} [{hint}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please answer yes or no.")


def prompt_choice(text: str, options: list[tuple[str, str]], *, default: str) -> str:
    print(text)
    for key, label in options:
        default_note = " (default)" if key == default else ""
        print(f"  {key}) {label}{default_note}")
    while True:
        value = input(f"Choose [{default}]: ").strip().lower()
        if not value:
            return default
        if any(key == value for key, _ in options):
            return value
        print("Please choose one of: " + ", ".join(key for key, _ in options))


def select_packages() -> list[str]:
    while True:
        selected: list[str] = []
        print("\nChoose the astronomy packages to receive:")
        for track_id in TRACK_IDS:
            if prompt_yes_no(f"Interested in {AU_STUDENT_TRACK_LABELS[track_id]}?", default=True):
                selected.append(track_id)
        if selected:
            return selected
        print("Pick at least one package.")


def post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        try:
            payload = json.loads(body)
            message = payload.get("error", exc.reason)
        except json.JSONDecodeError:
            message = body or exc.reason
        raise RuntimeError(str(message)) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach the student registry: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Student registry returned invalid JSON.") from exc


def manage_subscription(registry_url: str) -> int:
    action = prompt_choice(
        "\nWhat do you want to do?",
        [("1", "Create or update subscription"), ("2", "View current settings"), ("3", "Unsubscribe")],
        default="1",
    )
    email = prompt("Email")
    password = prompt_secret("Password")

    if action == "1":
        packages = select_packages()
        max_papers = prompt("Max papers per week", default=str(DEFAULT_MAX_PAPERS))
        new_password = prompt_optional_secret(
            "New password (leave blank to keep current password)"
        )
        result = post_json(
            registry_url,
            {
                "action": "upsert",
                "email": email,
                "password": password,
                "new_password": new_password,
                "package_ids": packages,
                "max_papers_per_week": int(max_papers),
            },
        )
        print(
            "\nSaved subscription for "
            f"{result['subscription']['email']} "
            f"({result['subscription']['max_papers_per_week']} papers/week)."
        )
        return 0

    if action == "2":
        result = post_json(
            registry_url,
            {"action": "get", "email": email, "password": password},
        )
        subscription = result["subscription"]
        labels = [AU_STUDENT_TRACK_LABELS[track_id] for track_id in subscription["package_ids"]]
        print(f"\nEmail: {subscription['email']}")
        print(f"Active: {subscription['active']}")
        print(f"Packages: {', '.join(labels)}")
        print(f"Max papers per week: {subscription['max_papers_per_week']}")
        return 0

    if prompt_yes_no(f"Stop sending the AU student digest to {email}?", default=False):
        post_json(
            registry_url,
            {"action": "unsubscribe", "email": email, "password": password},
        )
        print("\nUnsubscribed.")
    else:
        print("\nCancelled.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-url", default=DEFAULT_REGISTRY_URL)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return manage_subscription(args.registry_url)
    except RuntimeError as exc:
        print(f"\nError: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
