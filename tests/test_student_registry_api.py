import copy
import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "relay" / "api" / "students.py"
SPEC = importlib.util.spec_from_file_location("student_registry_api_test", MODULE_PATH)
students_api = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(students_api)


def test_registry_dispatch_lifecycle(monkeypatch):
    state = {"students": {}}
    saved = {}
    confirmation_calls = []

    def load_registry():
        return {"students": copy.deepcopy(state["students"])}, "sha-1"

    def save_registry(registry, sha, message):
        state["students"] = copy.deepcopy(registry["students"])
        saved["sha"] = sha
        saved["message"] = message

    monkeypatch.setattr(students_api, "_load_registry", load_registry)
    monkeypatch.setattr(students_api, "_save_registry", save_registry)
    monkeypatch.setattr(students_api, "STUDENT_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setattr(
        students_api,
        "_send_subscription_confirmation",
        lambda subscription, event: confirmation_calls.append((subscription["email"], event)) or (True, None),
    )

    status, payload = students_api._dispatch(
        {
            "action": "upsert",
            "email": "Student@Example.com",
            "password": "old-password",
            "package_ids": ["exoplanets"],
            "max_papers_per_week": 4,
        }
    )

    assert status == 200
    assert payload["subscription"]["email"] == "student@example.com"
    assert saved["message"] == "Update student subscription for student@example.com"
    assert payload["subscription_event"] == "created"
    assert payload["confirmation_email_sent"] is True
    assert confirmation_calls == [("student@example.com", "created")]

    with pytest.raises(PermissionError):
        students_api._handle_get(
            {"email": "student@example.com", "password": "wrong-password"}
        )

    status, payload = students_api._dispatch(
        {
            "action": "upsert",
            "email": "student@example.com",
            "password": "old-password",
            "new_password": "new-password",
            "package_ids": ["stars", "galaxies"],
            "max_papers_per_week": 3,
        }
    )

    assert status == 200
    assert payload["subscription"]["package_ids"] == ["stars", "galaxies"]
    assert payload["subscription"]["max_papers_per_week"] == 3
    assert payload["subscription_event"] == "updated"
    assert payload["confirmation_email_sent"] is False

    with pytest.raises(PermissionError):
        students_api._handle_get(
            {"email": "student@example.com", "password": "old-password"}
        )

    status, payload = students_api._dispatch(
        {
            "action": "get",
            "email": "student@example.com",
            "password": "new-password",
        }
    )
    assert status == 200
    assert payload["subscription"]["active"] is True

    status, payload = students_api._dispatch(
        {
            "action": "unsubscribe",
            "email": "student@example.com",
            "password": "new-password",
        }
    )
    assert status == 200
    assert payload["subscription"]["active"] is False

    status, payload = students_api._dispatch(
        {"action": "admin_list", "admin_token": "admin-secret"}
    )
    assert status == 200
    assert payload["subscriptions"] == []

    status, payload = students_api._dispatch(
        {
            "action": "upsert",
            "email": "student@example.com",
            "password": "new-password",
            "package_ids": ["stars"],
            "max_papers_per_week": 4,
        }
    )
    assert status == 200
    assert payload["subscription_event"] == "resubscribed"
    assert payload["confirmation_email_sent"] is True
    assert confirmation_calls[-1] == ("student@example.com", "resubscribed")

    status, payload = students_api._dispatch(
        {"action": "admin_list", "admin_token": "admin-secret"}
    )
    assert status == 200
    assert len(payload["subscriptions"]) == 1
    assert payload["subscriptions"][0]["active"] is True

    status, payload = students_api._dispatch(
        {
            "action": "admin_list",
            "admin_token": "admin-secret",
            "include_inactive": True,
        }
    )
    assert status == 200
    assert len(payload["subscriptions"]) == 1
    assert payload["subscriptions"][0]["active"] is True


def test_manage_page_includes_password_rotation_field():
    page = students_api._manage_page(
        "student@example.com",
        "unsubscribe",
        ["stars", "galaxies"],
        4,
    )

    assert "student@example.com" in page
    assert "New password (optional)" in page
    assert "Enter your password and click Unsubscribe." in page
    assert 'const initialPackages = ["stars", "galaxies"]' in page
    assert "const initialMaxPapers = 4" in page
    assert "Confirmed. First digest will arrive next Monday at 07:00 UTC." in page
    assert "A confirmation email has been sent." in page
