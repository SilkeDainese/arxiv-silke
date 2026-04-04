"""
tests/test_setup_server.py — Flask route tests for setup/server.py.

Covers every API endpoint introduced by the setup wizard backend.
All external I/O (ORCID, relay, AI APIs) is mocked so tests are offline.
"""

from __future__ import annotations

import json
import sys
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# Add setup/ to sys.path so `from server import app` works, and so server.py
# can itself import `from data import ARXIV_CATEGORIES, CATEGORY_HINTS`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "setup"))

import server  # noqa: E402 — must come after sys.path insert


# Register a crash-on-demand route before any request touches the app.
# The view function body is controlled by a mutable dict so each test can
# choose whether to raise or not without re-registering the route.
_crash_route_behaviour: dict = {"raise": False}


@server.app.route("/test-crash-route")
def _crash_view():
    if _crash_route_behaviour["raise"]:
        raise RuntimeError("deliberate test crash")
    from flask import jsonify
    return jsonify({"ok": True})


@pytest.fixture
def client():
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        yield c


# ─────────────────────────────────────────────────────────────
#  /api/orcid/lookup
# ─────────────────────────────────────────────────────────────


class TestOrcidLookup:
    def test_pure_unavailable_returns_503(self, client, monkeypatch):
        monkeypatch.setattr(server, "_PURE_AVAILABLE", False)
        resp = client.post("/api/orcid/lookup", json={"orcid_id": "0000-0001-2345-6789"})
        assert resp.status_code == 503

    def test_missing_orcid_returns_400(self, client, monkeypatch):
        monkeypatch.setattr(server, "_PURE_AVAILABLE", True)
        resp = client.post("/api/orcid/lookup", json={})
        assert resp.status_code == 400

    def test_invalid_format_returns_400(self, client, monkeypatch):
        monkeypatch.setattr(server, "_PURE_AVAILABLE", True)
        resp = client.post("/api/orcid/lookup", json={"orcid_id": "not-an-orcid"})
        assert resp.status_code == 400

    def test_full_url_format_is_accepted(self, client, monkeypatch):
        monkeypatch.setattr(server, "_PURE_AVAILABLE", True)

        def fake_person(orcid_id):
            return "Test User", "AU", None

        def fake_works(orcid_id):
            return {}, [], {}, {}, {}, None

        monkeypatch.setattr(server, "fetch_orcid_person", fake_person)
        monkeypatch.setattr(server, "fetch_orcid_works", fake_works)

        resp = client.post(
            "/api/orcid/lookup",
            json={"orcid_id": "https://orcid.org/0000-0001-2345-6789"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["name"] == "Test User"


# ─────────────────────────────────────────────────────────────
#  /api/categories
# ─────────────────────────────────────────────────────────────


class TestCategories:
    def test_returns_groups_hints_categories(self, client):
        resp = client.get("/api/categories")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "groups" in data
        assert "hints" in data
        assert "categories" in data

    def test_groups_contain_astrophysics(self, client):
        resp = client.get("/api/categories")
        data = resp.get_json()
        assert "Astrophysics" in data["groups"]
        assert "astro-ph.EP" in data["groups"]["Astrophysics"]

    def test_categories_has_all_astro_codes(self, client):
        resp = client.get("/api/categories")
        data = resp.get_json()
        for code in ["astro-ph.EP", "astro-ph.SR", "astro-ph.GA", "astro-ph.CO", "astro-ph.HE", "astro-ph.IM"]:
            assert code in data["categories"]

    def test_hints_match_group_names(self, client):
        resp = client.get("/api/categories")
        data = resp.get_json()
        for group_name in data["groups"]:
            assert group_name in data["hints"], f"Missing hint for group: {group_name}"

    def test_all_group_codes_exist_in_categories(self, client):
        resp = client.get("/api/categories")
        data = resp.get_json()
        for group_name, codes in data["groups"].items():
            for code in codes:
                assert code in data["categories"], f"{code} in group {group_name} not in categories"


# ─────────────────────────────────────────────────────────────
#  /api/ai/test-key
# ─────────────────────────────────────────────────────────────


class TestAiTestKey:
    def test_both_keys_empty_returns_ok_false(self, client):
        resp = client.post("/api/ai/test-key", json={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False

    def test_malformed_body_handled_gracefully(self, client):
        resp = client.post(
            "/api/ai/test-key",
            data="not-json",
            content_type="application/json",
        )
        # Flask's force=True returns 400 for syntactically invalid JSON — that is fine.
        assert resp.status_code in (200, 400)


# ─────────────────────────────────────────────────────────────
#  /api/ai/suggest
# ─────────────────────────────────────────────────────────────


class TestAiSuggest:
    def test_missing_description_returns_400(self, client):
        resp = client.post("/api/ai/suggest", json={})
        assert resp.status_code == 400
        assert "research_description" in resp.get_json()["error"]

    def test_description_returns_categories_and_keywords(self, client):
        resp = client.post(
            "/api/ai/suggest",
            json={"research_description": "I study exoplanet atmospheres and stellar spectra."},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "categories" in data
        assert "keywords" in data
        assert isinstance(data["categories"], list)
        assert isinstance(data["keywords"], dict)

    def test_regex_fallback_when_no_keys(self, client):
        # With no AI keys, the regex fallback must produce non-empty keywords.
        resp = client.post(
            "/api/ai/suggest",
            json={"research_description": "I study stellar rotation in open clusters using TESS photometry."},
        )
        assert resp.status_code == 200
        assert len(resp.get_json()["keywords"]) > 0


# ─────────────────────────────────────────────────────────────
#  /api/config/generate
# ─────────────────────────────────────────────────────────────


class TestConfigGenerate:
    def test_minimal_payload_returns_valid_yaml(self, client):
        resp = client.post("/api/config/generate", json={})
        assert resp.status_code == 200
        data = resp.get_json()
        cfg = yaml.safe_load(data["config_yaml"])
        assert isinstance(cfg, dict)
        assert "keywords" in cfg
        assert "cron_expr" in data

    def test_custom_schedule_returns_correct_cron(self, client):
        resp = client.post(
            "/api/config/generate",
            json={"schedule": "weekly", "send_hour_utc": 9},
        )
        assert resp.status_code == 200
        assert resp.get_json()["cron_expr"] == "0 9 * * 1"

    def test_weekday_schedule(self, client):
        resp = client.post(
            "/api/config/generate",
            json={"schedule": "weekdays", "send_hour_utc": 7},
        )
        assert resp.get_json()["cron_expr"] == "0 7 * * 1-5"

    def test_max_papers_and_min_score_optional(self, client):
        resp = client.post(
            "/api/config/generate",
            json={"max_papers": 10, "min_score": 3},
        )
        cfg = yaml.safe_load(resp.get_json()["config_yaml"])
        assert cfg["max_papers"] == 10
        assert cfg["min_score"] == 3


# ─────────────────────────────────────────────────────────────
#  /api/config/parse  ← new endpoint, zero prior tests
# ─────────────────────────────────────────────────────────────


class TestConfigParse:
    def test_valid_config_extracts_fields(self, client):
        cfg = {
            "researcher_name": "Silke",
            "categories": ["astro-ph.EP"],
            "keywords": {"exoplanet": 8, "transit": 6},
            "colleagues": {"people": ["Alice Smith"], "institutions": []},
            "digest_mode": "highlights",
        }
        resp = client.post("/api/config/parse", json={"yaml": yaml.dump(cfg)})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["researcher_name"] == "Silke"
        assert data["categories"] == ["astro-ph.EP"]
        assert data["keywords"]["exoplanet"] == 8
        assert "Alice Smith" in data["colleagues_people"]

    def test_empty_file_returns_400(self, client):
        resp = client.post("/api/config/parse", json={"yaml": "   "})
        assert resp.status_code == 400
        assert "Empty" in resp.get_json()["error"]

    def test_invalid_yaml_returns_400(self, client):
        resp = client.post("/api/config/parse", json={"yaml": "key: [unclosed"})
        assert resp.status_code == 400
        assert "YAML" in resp.get_json()["error"]

    def test_non_dict_yaml_returns_400(self, client):
        # A YAML file that is just a list is not a valid config
        resp = client.post("/api/config/parse", json={"yaml": "- item1\n- item2\n"})
        assert resp.status_code == 400
        assert "mapping" in resp.get_json()["error"].lower()

    def test_flat_colleagues_list_normalised_to_people_array(self, client):
        # Old format: colleagues is a flat list of names
        cfg_yaml = "colleagues:\n  - Alice\n  - Bob\n"
        resp = client.post("/api/config/parse", json={"yaml": cfg_yaml})
        assert resp.status_code == 200
        data = resp.get_json()
        assert "Alice" in data["colleagues_people"]
        assert "Bob" in data["colleagues_people"]

    def test_missing_optional_fields_return_defaults(self, client):
        cfg_yaml = "keywords:\n  exoplanet: 7\n"
        resp = client.post("/api/config/parse", json={"yaml": cfg_yaml})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["digest_mode"] == "highlights"
        assert data["schedule"] == "mon_wed_fri"
        assert data["categories"] == []

    def test_colleagues_null_handled_without_crash(self, client):
        cfg_yaml = "keywords:\n  exoplanet: 7\ncolleagues: null\n"
        resp = client.post("/api/config/parse", json={"yaml": cfg_yaml})
        assert resp.status_code == 200
        assert resp.get_json()["colleagues_people"] == []


# ─────────────────────────────────────────────────────────────
#  /api/invite/validate
# ─────────────────────────────────────────────────────────────


class TestInviteValidate:
    def test_no_invite_codes_set_returns_ok_false(self, client, monkeypatch):
        monkeypatch.setattr(server, "_INVITE_CODES", {})
        resp = client.post("/api/invite/validate", json={"code": "any-code"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is False

    def test_correct_code_returns_unlocked_keys(self, client, monkeypatch):
        monkeypatch.setattr(server, "_INVITE_CODES", {
            "secret-code": {"relay_token": "tok123", "gemini_api_key": "g-key", "anthropic_api_key": "a-key"},
        })
        resp = client.post("/api/invite/validate", json={"code": "secret-code"})
        data = resp.get_json()
        assert data["ok"] is True
        assert set(data["unlocked"]) == {"Relay", "Gemini", "Anthropic"}
        assert data["relay_token"] == "tok123"

    def test_wrong_code_returns_ok_false(self, client, monkeypatch):
        monkeypatch.setattr(server, "_INVITE_CODES", {"real-code": {"relay_token": "tok"}})
        resp = client.post("/api/invite/validate", json={"code": "wrong-code"})
        assert resp.get_json()["ok"] is False

    def test_empty_code_returns_ok_false(self, client, monkeypatch):
        monkeypatch.setattr(server, "_INVITE_CODES", {"real-code": {"relay_token": "tok"}})
        resp = client.post("/api/invite/validate", json={"code": ""})
        assert resp.get_json()["ok"] is False

    def test_partial_bundle_relay_only_lists_relay(self, client, monkeypatch):
        monkeypatch.setattr(server, "_INVITE_CODES", {
            "partial-code": {"relay_token": "tok", "gemini_api_key": "", "anthropic_api_key": ""},
        })
        resp = client.post("/api/invite/validate", json={"code": "partial-code"})
        data = resp.get_json()
        assert data["ok"] is True
        assert data["unlocked"] == ["Relay"]


# ─────────────────────────────────────────────────────────────
#  /api/students/register
# ─────────────────────────────────────────────────────────────


class TestStudentsRegister:
    def _make_relay_response(self, body: dict, status: int = 200):
        """Return a mock for urllib.request.urlopen that yields a JSON response."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(body).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_valid_au_email_forwarded_to_relay(self, client):
        relay_body = {"ok": True, "subscription": {"email": "au123456@uni.au.dk"}}
        mock_resp = self._make_relay_response(relay_body)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            resp = client.post(
                "/api/students/register",
                json={
                    "email": "au123456@uni.au.dk",
                    "password": "pass1234",
                    "package_ids": ["exoplanets"],
                    "max_papers_per_week": 4,
                },
            )
        assert resp.status_code == 200

    def test_non_au_email_returns_400(self, client):
        resp = client.post(
            "/api/students/register",
            json={"email": "user@gmail.com", "password": "pass1234", "package_ids": ["stars"]},
        )
        assert resp.status_code == 400
        assert "@uni.au.dk" in resp.get_json()["error"]

    def test_short_password_returns_400(self, client):
        resp = client.post(
            "/api/students/register",
            json={"email": "au123456@uni.au.dk", "password": "pw", "package_ids": ["stars"]},
        )
        assert resp.status_code == 400
        assert "short" in resp.get_json()["error"].lower()

    def test_no_packages_returns_400(self, client):
        resp = client.post(
            "/api/students/register",
            json={"email": "au123456@uni.au.dk", "password": "pass1234", "package_ids": []},
        )
        assert resp.status_code == 400

    def test_email_with_spaces_returns_400(self, client):
        # Regression test: " @uni.au.dk" suffix check with embedded spaces
        resp = client.post(
            "/api/students/register",
            json={"email": "au123 456@uni.au.dk", "password": "pass1234", "package_ids": ["stars"]},
        )
        assert resp.status_code == 400
        assert "Invalid email" in resp.get_json()["error"]

    def test_relay_unreachable_returns_502(self, client):
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            resp = client.post(
                "/api/students/register",
                json={
                    "email": "au123456@uni.au.dk",
                    "password": "pass1234",
                    "package_ids": ["stars"],
                },
            )
        assert resp.status_code == 502
        assert "relay" in resp.get_json()["error"].lower()

    def test_relay_error_response_forwarded(self, client):
        error_resp = MagicMock()
        error_resp.read.return_value = json.dumps({"error": "Student not found"}).encode()
        http_err = urllib.error.HTTPError(
            url="", code=404, msg="Not Found", hdrs={}, fp=BytesIO(b'{"error":"Student not found"}')
        )
        http_err.read = lambda: json.dumps({"error": "Student not found"}).encode()
        with patch("urllib.request.urlopen", side_effect=http_err):
            resp = client.post(
                "/api/students/register",
                json={
                    "email": "au123456@uni.au.dk",
                    "password": "pass1234",
                    "package_ids": ["stars"],
                },
            )
        assert resp.status_code == 404
        assert "Student not found" in resp.get_json()["error"]


# ─────────────────────────────────────────────────────────────
#  /api/orcid/lookup — timeout paths
# ─────────────────────────────────────────────────────────────


class TestOrcidLookupTimeouts:
    def test_orcid_lookup_person_timeout(self, client, monkeypatch):
        """fetch_orcid_person raising TimeoutError → 503 with friendly 'try again' message."""
        monkeypatch.setattr(server, "_PURE_AVAILABLE", True)

        def raise_timeout(orcid_id):
            raise TimeoutError("timed out")

        monkeypatch.setattr(server, "fetch_orcid_person", raise_timeout)
        resp = client.post("/api/orcid/lookup", json={"orcid_id": "0000-0001-2345-6789"})
        assert resp.status_code == 503
        assert "try again" in resp.get_json()["error"].lower()

    def test_orcid_lookup_works_timeout(self, client, monkeypatch):
        """fetch_orcid_works raising OSError → 503 with friendly 'try again' message."""
        monkeypatch.setattr(server, "_PURE_AVAILABLE", True)

        def fake_person(orcid_id):
            return "Test User", "AU", None

        def raise_oserror(orcid_id):
            raise OSError("connection reset")

        monkeypatch.setattr(server, "fetch_orcid_person", fake_person)
        monkeypatch.setattr(server, "fetch_orcid_works", raise_oserror)
        resp = client.post("/api/orcid/lookup", json={"orcid_id": "0000-0001-2345-6789"})
        assert resp.status_code == 503
        assert "try again" in resp.get_json()["error"].lower()


# ─────────────────────────────────────────────────────────────
#  Error handlers
# ─────────────────────────────────────────────────────────────


class TestErrorHandlers:
    def test_404_error_page(self, client):
        """Nonexistent route returns 404 with HTML containing 'not found'."""
        resp = client.get("/this/route/does/not/exist")
        assert resp.status_code == 404
        assert b"not found" in resp.data.lower()

    def test_500_error_page(self, client):
        """Unhandled exception in a route returns 500 with HTML containing 'went wrong'."""
        _crash_route_behaviour["raise"] = True
        server.app.config["TESTING"] = False
        try:
            resp = client.get("/test-crash-route")
            assert resp.status_code == 500
            assert b"went wrong" in resp.data.lower()
        finally:
            _crash_route_behaviour["raise"] = False
            server.app.config["TESTING"] = True

    def test_error_page_security_escaping(self, client):
        """Verify that malicious input in error pages is escaped (no SSTI/XSS)."""
        from server import _error_page
        with server.app.test_request_context():
            malicious_title = "<script>alert(1)</script>"
            malicious_message = "{{ 7*7 }}"
            malicious_tip = "{% if True %}Exploit{% endif %}"

            html_content, code = _error_page(
                500, malicious_title, malicious_message, [malicious_tip]
            )

            # XSS Check: Title should be escaped
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_content
            assert "<script>alert(1)</script>" not in html_content

            # SSTI Check: Message and Tips should be rendered literally
            assert "{{ 7*7 }}" in html_content
            assert "49" not in html_content
            assert "{% if True %}Exploit{% endif %}" in html_content
