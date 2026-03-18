"""
tests/test_relay_stability.py — Relay crash-resistance tests.

Covers the malformed-JSON-from-GitHub paths in students.py and feedback.py
where GitHub could theoretically return a JSON list instead of a dict.
All tests mock _github_request so no real network calls are made.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import patch

import pytest


def _b64(obj) -> str:
    """Encode a Python object to the base64-wrapped string GitHub returns."""
    return base64.b64encode(json.dumps(obj).encode()).decode()


# ─────────────────────────────────────────────────────────────
#  relay/api/students — malformed registry (list instead of dict)
# ─────────────────────────────────────────────────────────────


class TestStudentsRegistryMalformed:
    def test_list_registry_does_not_crash(self):
        """GitHub returning a JSON list for the registry must not crash the loader."""
        import relay.api.students as students_mod

        fake_response = {"content": _b64([1, 2, 3]), "sha": "abc123"}
        with patch.object(students_mod, "_github_request", return_value=fake_response):
            registry, sha = students_mod._load_registry()

        assert isinstance(registry, dict)
        assert "students" in registry
        assert sha == "abc123"

    def test_list_registry_returns_empty_students(self):
        """A malformed list registry must produce an empty students dict, not partial data."""
        import relay.api.students as students_mod

        fake_response = {"content": _b64(["garbage", "data"]), "sha": None}
        with patch.object(students_mod, "_github_request", return_value=fake_response):
            registry, _ = students_mod._load_registry()

        assert registry["students"] == {}


# ─────────────────────────────────────────────────────────────
#  relay/api/feedback — malformed store (list instead of dict)
# ─────────────────────────────────────────────────────────────


class TestFeedbackStoreMalformed:
    def test_list_store_does_not_crash(self):
        """GitHub returning a JSON list for the feedback store must not crash the loader."""
        import relay.api.feedback as feedback_mod

        fake_response = {"content": _b64([{"vote": "up"}]), "sha": "def456"}
        with patch.object(feedback_mod, "_github_request", return_value=fake_response):
            store, sha = feedback_mod._load_feedback_store()

        assert isinstance(store, dict)
        assert "votes" in store
        assert "aggregated" in store
        assert sha == "def456"

    def test_list_store_returns_empty_defaults(self):
        """A malformed list store must produce empty votes/aggregated, not partial data."""
        import relay.api.feedback as feedback_mod

        fake_response = {"content": _b64([1, 2, 3]), "sha": None}
        with patch.object(feedback_mod, "_github_request", return_value=fake_response):
            store, _ = feedback_mod._load_feedback_store()

        assert store["votes"] == []
        assert store["aggregated"] == {}
