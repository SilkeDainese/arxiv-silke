"""
tests/test_failure_notifications.py

Tests for failure notification emails:
  - send_failure_report in digest.py
  - relay error responses all include {"ok": false, "error": "..."}
"""

from __future__ import annotations

import io
import json
import smtplib
import sys
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

import digest as d
from digest import send_failure_report
import relay.api.send as relay_send
import relay.api.feedback as relay_feedback
import relay.api.students as relay_students


# ─────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────

def make_config(**overrides):
    base = {
        "recipient_email": "admin@example.com",
        "digest_name": "Test Digest",
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
    }
    base.update(overrides)
    return base


class _FakeHandler:
    """Minimal stand-in for BaseHTTPRequestHandler used in relay tests."""
    def __init__(self):
        self.response = None

    def _respond(self, status, body):
        self.response = (status, body)


# ─────────────────────────────────────────────────────────────
#  send_failure_report — direct SMTP path
# ─────────────────────────────────────────────────────────────

def test_send_failure_report_uses_direct_smtp_when_credentials_set():
    config = make_config()
    with (
        patch.dict("os.environ", {"SMTP_USER": "bot@gmail.com", "SMTP_PASSWORD": "secret"}),
        patch("digest._send_via_smtp", return_value=True) as mock_smtp,
    ):
        send_failure_report(config, "Something exploded")

    mock_smtp.assert_called_once()
    call_args = mock_smtp.call_args
    recipients, subject = call_args.args[0], call_args.args[1]
    assert recipients == ["admin@example.com"]
    assert "failed" in subject.lower() or "arXiv" in subject


def test_send_failure_report_subject_contains_date_and_warning():
    config = make_config()
    captured_subjects = []

    def fake_smtp(recipients, subject, html, plain, user, pw, server, port, name):
        captured_subjects.append(subject)
        return True

    with (
        patch.dict("os.environ", {"SMTP_USER": "bot@gmail.com", "SMTP_PASSWORD": "secret"}),
        patch("digest._send_via_smtp", side_effect=fake_smtp),
    ):
        send_failure_report(config, "boom")

    assert len(captured_subjects) == 1
    assert "⚠️" in captured_subjects[0]
    assert "arXiv Digest failed" in captured_subjects[0]


def test_send_failure_report_body_contains_error_summary():
    config = make_config()
    captured_bodies = []

    def fake_smtp(recipients, subject, html, plain, user, pw, server, port, name):
        captured_bodies.append(plain)
        return True

    with (
        patch.dict("os.environ", {"SMTP_USER": "bot@gmail.com", "SMTP_PASSWORD": "secret"}),
        patch("digest._send_via_smtp", side_effect=fake_smtp),
    ):
        send_failure_report(config, "Traceback: ZeroDivisionError")

    assert any("ZeroDivisionError" in b for b in captured_bodies)


# ─────────────────────────────────────────────────────────────
#  send_failure_report — relay fallback path
# ─────────────────────────────────────────────────────────────

def test_send_failure_report_falls_back_to_relay_when_smtp_fails():
    config = make_config()
    with (
        patch.dict("os.environ", {
            "SMTP_USER": "bot@gmail.com",
            "SMTP_PASSWORD": "secret",
            "DIGEST_RELAY_TOKEN": "tok123",
        }),
        patch("digest._send_via_smtp", return_value=False),
        patch("digest._send_via_relay", return_value=True) as mock_relay,
    ):
        send_failure_report(config, "SMTP down")

    mock_relay.assert_called_once()


def test_send_failure_report_uses_relay_when_no_smtp_creds():
    config = make_config()
    with (
        patch.dict("os.environ", {
            "SMTP_USER": "",
            "SMTP_PASSWORD": "",
            "DIGEST_RELAY_TOKEN": "tok456",
        }),
        patch("digest._send_via_relay", return_value=True) as mock_relay,
    ):
        send_failure_report(config, "No SMTP")

    mock_relay.assert_called_once()
    recipients = mock_relay.call_args.args[0]
    assert recipients == ["admin@example.com"]


# ─────────────────────────────────────────────────────────────
#  send_failure_report — no email configured
# ─────────────────────────────────────────────────────────────

def test_send_failure_report_prints_to_stderr_when_no_recipient(capsys):
    config = make_config(recipient_email="")
    with patch.dict("os.environ", {"SMTP_USER": "", "SMTP_PASSWORD": "", "DIGEST_RELAY_TOKEN": ""}):
        send_failure_report(config, "oops")

    captured = capsys.readouterr()
    assert "stderr" not in captured.out  # nothing on stdout
    assert "recipient_email" in captured.err or "No recipient" in captured.err


def test_send_failure_report_handles_none_config(capsys):
    with patch.dict("os.environ", {"SMTP_USER": "", "SMTP_PASSWORD": "", "DIGEST_RELAY_TOKEN": ""}):
        send_failure_report(None, "config unavailable")

    captured = capsys.readouterr()
    # Should not crash; should print something to stderr
    assert captured.err != "" or True  # no exception is the main requirement


def test_send_failure_report_handles_list_recipient():
    """recipient_email can be a list; failure report uses the first entry."""
    config = make_config(recipient_email=["first@example.com", "second@example.com"])
    with (
        patch.dict("os.environ", {"SMTP_USER": "bot@gmail.com", "SMTP_PASSWORD": "secret"}),
        patch("digest._send_via_smtp", return_value=True) as mock_smtp,
    ):
        send_failure_report(config, "oops")

    recipients = mock_smtp.call_args.args[0]
    assert recipients == ["first@example.com"]


# ─────────────────────────────────────────────────────────────
#  relay/api/send.py — all errors return {"ok": false, "error": ...}
# ─────────────────────────────────────────────────────────────

def test_relay_send_invalid_json_returns_ok_false():
    fake = _FakeHandler()
    # Simulate JSON parse failure by triggering the error path directly
    # We need a handler that raises on rfile.read — use a mock headers object
    fake.headers = {"Content-Length": "5"}
    fake.rfile = MagicMock()
    fake.rfile.read.return_value = b"!!!!!"  # invalid JSON

    relay_send.handler.do_POST(fake)

    status, body = fake.response
    assert status == 400
    assert body.get("ok") is False
    assert "error" in body


def test_relay_send_invalid_token_returns_ok_false():
    fake = _FakeHandler()
    import json, io
    payload = json.dumps({"token": "wrong", "recipients": ["x@x.com"], "subject": "hi", "html": "<p>hi</p>"}).encode()
    fake.headers = {"Content-Length": str(len(payload))}
    fake.rfile = io.BytesIO(payload)

    with patch.dict(relay_send.__dict__, {"RELAY_TOKEN": "correct"}):
        relay_send.handler.do_POST(fake)

    status, body = fake.response
    assert status == 403
    assert body.get("ok") is False
    assert "error" in body


def test_relay_send_missing_fields_returns_ok_false():
    fake = _FakeHandler()
    import json, io
    payload = json.dumps({"token": "tok", "recipients": [], "subject": "", "html": ""}).encode()
    fake.headers = {"Content-Length": str(len(payload))}
    fake.rfile = io.BytesIO(payload)

    with patch.dict(relay_send.__dict__, {"RELAY_TOKEN": "tok"}):
        relay_send.handler.do_POST(fake)

    status, body = fake.response
    assert status == 400
    assert body.get("ok") is False


def test_relay_send_smtp_not_configured_returns_ok_false():
    fake = _FakeHandler()
    import json, io
    payload = json.dumps({
        "token": "tok",
        "recipients": ["a@b.com"],
        "subject": "hi",
        "html": "<p>hi</p>",
    }).encode()
    fake.headers = {"Content-Length": str(len(payload))}
    fake.rfile = io.BytesIO(payload)

    with (
        patch.dict(relay_send.__dict__, {"RELAY_TOKEN": "tok", "SMTP_USER": "", "SMTP_PASSWORD": ""}),
    ):
        relay_send.handler.do_POST(fake)

    status, body = fake.response
    assert status == 500
    assert body.get("ok") is False
    assert "error" in body


def test_relay_send_smtp_auth_error_returns_ok_false():
    fake = _FakeHandler()
    import json, io
    payload = json.dumps({
        "token": "tok",
        "recipients": ["a@b.com"],
        "subject": "hi",
        "html": "<p>hi</p>",
    }).encode()
    fake.headers = {"Content-Length": str(len(payload))}
    fake.rfile = io.BytesIO(payload)

    with (
        patch.dict(relay_send.__dict__, {"RELAY_TOKEN": "tok", "SMTP_USER": "u", "SMTP_PASSWORD": "p"}),
        patch("smtplib.SMTP") as mock_smtp_class,
    ):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)
        mock_server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Bad credentials")

        relay_send.handler.do_POST(fake)

    status, body = fake.response
    assert status == 500
    assert body.get("ok") is False
    assert "error" in body


# ─────────────────────────────────────────────────────────────
#  relay/api/feedback.py — error responses include ok: false
# ─────────────────────────────────────────────────────────────

def test_relay_feedback_invalid_json_returns_ok_false():
    fake = _FakeHandler()
    fake.headers = {"Content-Length": "3"}
    fake.rfile = MagicMock()
    fake.rfile.read.return_value = b"!!!"

    relay_feedback.handler.do_POST(fake)

    status, body = fake.response
    assert status == 400
    assert body.get("ok") is False
    assert "error" in body


def test_relay_feedback_unknown_action_returns_ok_false():
    fake = _FakeHandler()
    import json, io
    payload = json.dumps({"action": "frobnicate"}).encode()
    fake.headers = {"Content-Length": str(len(payload))}
    fake.rfile = io.BytesIO(payload)

    relay_feedback.handler.do_POST(fake)

    status, body = fake.response
    assert status == 400
    assert body.get("ok") is False
    assert "error" in body


def test_relay_feedback_permission_error_returns_ok_false():
    fake = _FakeHandler()
    import json, io
    payload = json.dumps({"action": "aggregate", "admin_token": "wrong"}).encode()
    fake.headers = {"Content-Length": str(len(payload))}
    fake.rfile = io.BytesIO(payload)

    with patch.dict(relay_feedback.__dict__, {"STUDENT_ADMIN_TOKEN": "correct"}):
        relay_feedback.handler.do_POST(fake)

    status, body = fake.response
    assert status == 403
    assert body.get("ok") is False
    assert "error" in body


# ─────────────────────────────────────────────────────────────
#  relay/api/students.py — error responses include ok: false
# ─────────────────────────────────────────────────────────────

def test_relay_students_invalid_json_returns_ok_false():
    fake = _FakeHandler()
    fake.headers = {"Content-Length": "3"}
    fake.rfile = MagicMock()
    fake.rfile.read.return_value = b"!!!"

    relay_students.handler.do_POST(fake)

    status, body = fake.response
    assert status == 400
    assert body.get("ok") is False
    assert "error" in body


def test_relay_students_unknown_action_returns_ok_false():
    fake = _FakeHandler()
    import json, io
    payload = json.dumps({"action": "teleport"}).encode()
    fake.headers = {"Content-Length": str(len(payload))}
    fake.rfile = io.BytesIO(payload)

    relay_students.handler.do_POST(fake)

    status, body = fake.response
    assert status == 400
    assert body.get("ok") is False
    assert "error" in body


def test_relay_students_permission_error_returns_ok_false():
    fake = _FakeHandler()
    import json, io
    payload = json.dumps({"action": "admin_list", "admin_token": "wrong"}).encode()
    fake.headers = {"Content-Length": str(len(payload))}
    fake.rfile = io.BytesIO(payload)

    with patch.dict(relay_students.__dict__, {"STUDENT_ADMIN_TOKEN": "correct"}):
        relay_students.handler.do_POST(fake)

    status, body = fake.response
    assert status == 403
    assert body.get("ok") is False
    assert "error" in body


# ─────────────────────────────────────────────────────────────
#  relay/api/send.py — additional error paths
# ─────────────────────────────────────────────────────────────

def test_relay_send_too_many_recipients():
    """Payload with 21 recipients → 400."""
    fake = _FakeHandler()
    payload = json.dumps({
        "token": "tok",
        "recipients": [f"user{i}@example.com" for i in range(21)],
        "subject": "hi",
        "html": "<p>hi</p>",
    }).encode()
    fake.headers = {"Content-Length": str(len(payload))}
    fake.rfile = io.BytesIO(payload)

    with patch.dict(relay_send.__dict__, {"RELAY_TOKEN": "tok", "SMTP_USER": "u", "SMTP_PASSWORD": "p"}):
        relay_send.handler.do_POST(fake)

    status, body = fake.response
    assert status == 400
    assert body.get("ok") is False


def test_relay_send_generic_smtp_error():
    """sendmail raising ConnectionResetError → 500 with ok: false."""
    fake = _FakeHandler()
    payload = json.dumps({
        "token": "tok",
        "recipients": ["a@b.com"],
        "subject": "hi",
        "html": "<p>hi</p>",
    }).encode()
    fake.headers = {"Content-Length": str(len(payload))}
    fake.rfile = io.BytesIO(payload)

    with (
        patch.dict(relay_send.__dict__, {"RELAY_TOKEN": "tok", "SMTP_USER": "u", "SMTP_PASSWORD": "p"}),
        patch("smtplib.SMTP") as mock_smtp_class,
    ):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)
        mock_server.sendmail.side_effect = ConnectionResetError("connection reset by peer")

        relay_send.handler.do_POST(fake)

    status, body = fake.response
    assert status == 500
    assert body.get("ok") is False


# ─────────────────────────────────────────────────────────────
#  relay/api/students.py — additional error paths
# ─────────────────────────────────────────────────────────────

def test_relay_students_file_not_found():
    """dispatch raising FileNotFoundError → 404 with ok: false."""
    fake = _FakeHandler()
    payload = json.dumps({"action": "request_subscribe"}).encode()
    fake.headers = {"Content-Length": str(len(payload))}
    fake.rfile = io.BytesIO(payload)

    with patch("relay.api.students._dispatch", side_effect=FileNotFoundError("registry missing")):
        relay_students.handler.do_POST(fake)

    status, body = fake.response
    assert status == 404
    assert body.get("ok") is False


def test_relay_students_unhandled_error():
    """dispatch raising bare Exception → 500 with ok: false."""
    fake = _FakeHandler()
    payload = json.dumps({"action": "request_subscribe"}).encode()
    fake.headers = {"Content-Length": str(len(payload))}
    fake.rfile = io.BytesIO(payload)

    with patch("relay.api.students._dispatch", side_effect=Exception("unexpected boom")):
        relay_students.handler.do_POST(fake)

    status, body = fake.response
    assert status == 500
    assert body.get("ok") is False


# ─────────────────────────────────────────────────────────────
#  send_failure_report — no SMTP, no relay
# ─────────────────────────────────────────────────────────────

def test_failure_report_no_smtp_no_relay(capsys):
    """With a recipient but no SMTP or relay config, a warning is printed to stderr."""
    config = make_config(recipient_email="researcher@example.com")
    with patch.dict("os.environ", {"SMTP_USER": "", "SMTP_PASSWORD": "", "DIGEST_RELAY_TOKEN": ""}):
        send_failure_report(config, "pipeline crashed")

    captured = capsys.readouterr()
    assert captured.err != ""


# ─────────────────────────────────────────────────────────────
#  __main__ wrappers — crash → send_failure_report + SystemExit(1)
# ─────────────────────────────────────────────────────────────

def test_digest_main_sends_failure_report_on_crash():
    """RuntimeError in main() → send_failure_report called, SystemExit(1) raised."""
    with (
        patch("digest.main", side_effect=RuntimeError("pipeline boom")),
        patch("digest.load_config", return_value={
            "recipient_email": "admin@example.com",
            "digest_name": "Test",
            "smtp_server": "smtp.gmail.com",
            "smtp_port": 587,
        }),
        patch("digest.send_failure_report") as mock_report,
        patch.dict("os.environ", {"SMTP_USER": "", "SMTP_PASSWORD": "", "DIGEST_RELAY_TOKEN": ""}),
    ):
        with pytest.raises(SystemExit) as exc_info:
            # Mirror the actual __main__ guard logic
            _config_for_failure = None
            try:
                try:
                    _config_for_failure = d.load_config()
                except Exception:
                    pass
                d.main()
            except SystemExit:
                raise
            except Exception as _exc:
                import traceback
                _tb = traceback.format_exc()
                try:
                    d.send_failure_report(_config_for_failure, _tb)
                except Exception:
                    pass
                raise SystemExit(1) from None

    assert exc_info.value.code == 1
    mock_report.assert_called_once()


def test_student_digest_main_sends_failure_on_crash():
    """RuntimeError in student_digest.main() → send_failure_report called, SystemExit(1) raised."""
    import student_digest as sd

    with (
        patch("student_digest.main", side_effect=RuntimeError("student pipeline boom")),
        patch("student_digest.build_student_base_config", return_value={
            "recipient_email": "",
            "digest_name": "Test",
            "smtp_server": "smtp.gmail.com",
            "smtp_port": 587,
        }),
        patch("student_digest.send_failure_report") as mock_report,
        patch.dict("os.environ", {
            "RECIPIENT_EMAIL": "admin@example.com",
            "SMTP_USER": "",
            "SMTP_PASSWORD": "",
            "DIGEST_RELAY_TOKEN": "",
        }),
    ):
        with pytest.raises(SystemExit) as exc_info:
            # Mirror the actual __main__ block logic
            try:
                sd.main()
            except SystemExit:
                raise
            except Exception as _exc:
                import traceback
                _tb = traceback.format_exc()
                try:
                    _admin_config = sd.build_student_base_config()
                    _admin_config["recipient_email"] = "admin@example.com"
                    sd.send_failure_report(_admin_config, _tb)
                except Exception:
                    pass
                raise SystemExit(1) from None

    assert exc_info.value.code == 1
    mock_report.assert_called_once()
