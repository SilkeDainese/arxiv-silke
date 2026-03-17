"""Tests for the failure report relay API."""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

import pytest


# ─────── Helpers ──────────────────────────────────────────────

VALID_REPORT = {
    "repo": "student-fork/arxiv-digest",
    "run_id": "987654321",
    "error": "SMTP authentication failed",
    "workflow": "digest",
    "timestamp": "2026-03-17T08:00:00+00:00",
}


class _FakeHandler:
    """Minimal stand-in for BaseHTTPRequestHandler used by handler methods."""

    def __init__(self, body: dict[str, Any] | None = None):
        import io
        raw = json.dumps(body or {}).encode("utf-8")
        self.rfile = io.BytesIO(raw)
        self.headers = {"Content-Length": str(len(raw))}
        self.status = None
        self.response_body = None
        self._headers_sent: list[tuple[str, str]] = []

    def _respond(self, status: int, body: dict[str, Any]):
        self.status = status
        self.response_body = body

    # Stubs for BaseHTTPRequestHandler write methods (used by handler._respond)
    def send_response(self, code: int):
        self.status = code

    def send_header(self, key: str, value: str):
        self._headers_sent.append((key, value))

    def end_headers(self):
        pass

    @property
    def wfile(self):
        import io
        return io.BytesIO()


# ─────── Validation tests ────────────────────────────────────

class TestReportValidation:
    """Missing or empty required fields must return 400."""

    @pytest.mark.parametrize("field", ["repo", "run_id", "error", "timestamp", "workflow"])
    def test_missing_field_returns_400(self, field: str):
        from relay.api.report import _handle_report

        body = {k: v for k, v in VALID_REPORT.items() if k != field}
        status, payload = _handle_report(body)

        assert status == 400
        assert field in payload["error"]

    def test_empty_field_returns_400(self):
        from relay.api.report import _handle_report

        body = {**VALID_REPORT, "repo": "  "}
        status, payload = _handle_report(body)

        assert status == 400
        assert "repo" in payload["error"]


# ─────── Success path ────────────────────────────────────────

class TestReportSuccess:
    """A valid report stores the failure and creates an upstream issue."""

    def test_valid_report_stores_and_creates_issue(self):
        from relay.api.report import _handle_report

        fake_issue = {"html_url": "https://github.com/SilkeDainese/arxiv-digest/issues/42"}

        with (
            mock.patch("relay.api.report._load_report_store", return_value=([], "sha1")),
            mock.patch("relay.api.report._save_report_store") as mock_save,
            mock.patch("relay.api.report._create_issue", return_value=fake_issue["html_url"]) as mock_issue,
        ):
            status, payload = _handle_report(VALID_REPORT)

        assert status == 200
        assert payload["ok"] is True
        assert payload["issue_url"] == fake_issue["html_url"]

        # Verify the failure was appended to the store
        saved_store = mock_save.call_args[0][0]
        assert len(saved_store) == 1
        assert saved_store[0]["repo"] == VALID_REPORT["repo"]
        assert saved_store[0]["run_id"] == VALID_REPORT["run_id"]

        # Verify issue creation received the right arguments
        mock_issue.assert_called_once_with(
            VALID_REPORT["repo"],
            VALID_REPORT["run_id"],
            VALID_REPORT["error"],
            VALID_REPORT["workflow"],
        )

    def test_report_appends_to_existing_store(self):
        from relay.api.report import _handle_report

        existing = [{"repo": "old/repo", "run_id": "111", "error": "x", "workflow": "w", "timestamp": "t"}]

        with (
            mock.patch("relay.api.report._load_report_store", return_value=(existing, "sha2")),
            mock.patch("relay.api.report._save_report_store") as mock_save,
            mock.patch("relay.api.report._create_issue", return_value="https://github.com/issues/1"),
        ):
            status, _ = _handle_report(VALID_REPORT)

        assert status == 200
        saved_store = mock_save.call_args[0][0]
        assert len(saved_store) == 2


# ─────── Security / stability tests ────────────────────────

class TestReportSecurity:
    """New guards: repo format, run_id format, deduplication, store cap, token."""

    def test_invalid_repo_format_returns_400(self):
        from relay.api.report import _handle_report

        body = {**VALID_REPORT, "repo": "not-a/valid/repo/path"}
        status, payload = _handle_report(body)

        assert status == 400
        assert "repo" in payload["error"].lower()

    def test_non_numeric_run_id_returns_400(self):
        from relay.api.report import _handle_report

        body = {**VALID_REPORT, "run_id": "../../etc/passwd"}
        status, payload = _handle_report(body)

        assert status == 400
        assert "run_id" in payload["error"].lower()

    def test_duplicate_repo_run_id_is_skipped(self):
        from relay.api.report import _handle_report

        existing = [dict(VALID_REPORT)]
        with (
            mock.patch("relay.api.report._load_report_store", return_value=(existing, "sha1")),
            mock.patch("relay.api.report._save_report_store") as mock_save,
            mock.patch("relay.api.report._create_issue") as mock_issue,
        ):
            status, payload = _handle_report(VALID_REPORT)

        assert status == 200
        assert payload.get("skipped") == "already reported"
        mock_save.assert_not_called()
        mock_issue.assert_not_called()

    def test_store_is_capped_at_max_entries(self):
        from relay.api import report as report_mod
        from relay.api.report import _handle_report

        oversized = [
            {"repo": f"fork-{i}/arxiv-digest", "run_id": str(i), "error": "x", "workflow": "w", "timestamp": "t"}
            for i in range(report_mod._MAX_STORE_ENTRIES + 10)
        ]
        with (
            mock.patch("relay.api.report._load_report_store", return_value=(oversized, "sha1")),
            mock.patch("relay.api.report._save_report_store") as mock_save,
            mock.patch("relay.api.report._create_issue", return_value="https://github.com/issues/1"),
        ):
            _handle_report(VALID_REPORT)

        saved_store = mock_save.call_args[0][0]
        assert len(saved_store) <= report_mod._MAX_STORE_ENTRIES

    def test_backtick_injection_in_error_is_escaped(self):
        from relay.api.report import _sanitise_error

        malicious = "normal text\n```\n# injected heading\n```"
        result = _sanitise_error(malicious)
        assert "```" not in result

    def test_error_is_truncated_to_max_length(self):
        from relay.api import report as report_mod
        from relay.api.report import _sanitise_error

        long_error = "x" * (report_mod._MAX_ERROR_LEN + 500)
        result = _sanitise_error(long_error)
        assert len(result) <= report_mod._MAX_ERROR_LEN

    def test_token_guard_rejects_when_token_set(self):
        from relay.api.report import _handle_report

        with mock.patch("relay.api.report.REPORT_RELAY_TOKEN", "secret123"):
            status, payload = _handle_report({**VALID_REPORT, "token": "wrong"})

        assert status == 200
        assert payload.get("skipped") == "token required"

    def test_token_guard_passes_when_correct(self):
        from relay.api.report import _handle_report

        with (
            mock.patch("relay.api.report.REPORT_RELAY_TOKEN", "secret123"),
            mock.patch("relay.api.report._load_report_store", return_value=([], "sha1")),
            mock.patch("relay.api.report._save_report_store"),
            mock.patch("relay.api.report._create_issue", return_value="https://github.com/issues/1"),
        ):
            status, payload = _handle_report({**VALID_REPORT, "token": "secret123"})

        assert status == 200
        assert payload["ok"] is True

    def test_token_guard_bypassed_when_no_token_configured(self):
        """When REPORT_RELAY_TOKEN is not set, any caller is accepted."""
        from relay.api.report import _handle_report

        with (
            mock.patch("relay.api.report.REPORT_RELAY_TOKEN", ""),
            mock.patch("relay.api.report._load_report_store", return_value=([], "sha1")),
            mock.patch("relay.api.report._save_report_store"),
            mock.patch("relay.api.report._create_issue", return_value="https://github.com/issues/1"),
        ):
            status, payload = _handle_report(VALID_REPORT)

        assert status == 200
        assert payload["ok"] is True


# ─────── Handler class tests ─────────────────────────────────

class TestHandlerPost:
    """The Vercel handler class routes POST requests correctly."""

    def test_post_delegates_to_handle_report(self):
        from relay.api.report import handler

        fake = _FakeHandler(VALID_REPORT)
        fake_issue_url = "https://github.com/SilkeDainese/arxiv-digest/issues/99"

        with (
            mock.patch(
                "relay.api.report._handle_report",
                return_value=(200, {"ok": True, "issue_url": fake_issue_url}),
            ) as mock_handle,
        ):
            handler.do_POST(fake)

        mock_handle.assert_called_once()
        assert fake.status == 200

    def test_post_missing_fields_returns_400(self):
        from relay.api.report import handler

        body = {k: v for k, v in VALID_REPORT.items() if k != "repo"}
        fake = _FakeHandler(body)

        with mock.patch(
            "relay.api.report._handle_report",
            return_value=(400, {"error": "Missing required fields: repo"}),
        ):
            handler.do_POST(fake)

        assert fake.status == 400
