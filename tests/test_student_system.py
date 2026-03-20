import urllib.error

import student_digest as sd
from digest import _render_footer
from setup.student_presets import (
    build_au_student_config,
    build_au_student_manage_url,
    build_au_student_subscription_preview,
)
from student_digest import annotate_student_packages, make_student_digest_config, select_student_papers
from student_registry import (
    build_student_record,
    normalise_package_ids,
    normalise_public_subscription,
    public_record,
)


def make_paper(**overrides):
    paper = {
        "id": "p1",
        "title": "Paper",
        "category": "astro-ph.EP",
        "matched_keywords": ["exoplanet"],
        "colleague_matches": [],
        "relevance_score": 7,
        "feedback_bias": 0,
        "published": "2026-03-15",
        "student_package_ids": [],
        "student_au_priority": 0,
    }
    paper.update(overrides)
    return paper


def test_build_student_record_creates_passwordless_record():
    record = build_student_record(
        email="Student@Example.com",
        package_ids=["exoplanets", "galaxies"],
        max_papers_per_week=9,
    )

    assert record["email"] == "student@example.com"
    assert record["package_ids"] == ["exoplanets", "galaxies"]
    assert record["max_papers_per_week"] == 9
    assert record["active"] is True
    assert "password_salt" not in record
    assert "password_hash" not in record


def test_build_student_record_updates_existing():
    original = build_student_record(
        email="student@example.com",
        package_ids=["exoplanets"],
        max_papers_per_week=6,
    )

    updated = build_student_record(
        email="student@example.com",
        package_ids=["stars", "galaxies"],
        max_papers_per_week=4,
        existing=original,
    )

    assert updated["package_ids"] == ["stars", "galaxies"]
    assert updated["max_papers_per_week"] == 4
    assert updated["created_at"] == original["created_at"]


def test_normalise_package_ids_rejects_empty():
    try:
        normalise_package_ids([])
    except ValueError as exc:
        assert "Pick at least one" in str(exc)
    else:
        raise AssertionError("Expected ValueError for empty package list")


def test_annotate_student_packages_and_rank_au_first():
    papers = [
        make_paper(id="plain-high", relevance_score=9, matched_keywords=["exoplanet"]),
        make_paper(
            id="au-mid",
            relevance_score=6,
            matched_keywords=["SONG"],
            category="astro-ph.SR",
        ),
    ]

    annotate_student_packages(papers)
    selected = select_student_papers(papers, ["exoplanets", "stars"], 2)

    assert papers[0]["student_package_ids"] == ["exoplanets"]
    assert "stars" in papers[1]["student_package_ids"]  # astro-ph.SR also maps to solar_helio now
    assert selected[0]["id"] == "au-mid"
    assert selected[1]["id"] == "plain-high"


def test_make_student_digest_config_adds_manage_links():
    config = make_student_digest_config(
        {"digest_name": "AU Astronomy Student Weekly", "tagline": "", "max_papers": 20},
        {
            "email": "student@example.com",
            "package_ids": ["exoplanets", "galaxies"],
            "max_papers_per_week": 5,
        },
    )

    assert config["recipient_email"] == "student@example.com"
    assert "email=student%40example.com" in config["subscription_manage_url"]
    assert "mode=unsubscribe" in config["subscription_unsubscribe_url"]
    assert "Planets & exoplanets" in config["tagline"]
    assert "Galaxies" in config["tagline"]


def test_make_student_digest_config_manage_url_has_no_stale_packages():
    """Manage URL in weekly digest must not pre-fill packages or max_papers.

    Pre-filling from the URL embeds stale data: if a student updates their
    subscription mid-week and then clicks an older email's manage link, the
    form silently overwrites their current choices. Email-only URL forces
    them to load current settings before saving.
    """
    import urllib.parse
    config = make_student_digest_config(
        {"digest_name": "AU Astronomy Student Weekly", "tagline": "", "max_papers": 20},
        {
            "email": "student@example.com",
            "package_ids": ["exoplanets", "galaxies"],
            "max_papers_per_week": 5,
        },
    )
    params = urllib.parse.parse_qs(urllib.parse.urlparse(config["subscription_manage_url"]).query)
    assert "packages" not in params, "Manage URL must not pre-fill packages (stale overwrite risk)"
    assert "max_papers" not in params, "Manage URL must not pre-fill max_papers (stale overwrite risk)"
    assert "email" in params, "Manage URL must include email for convenience"


def test_public_record_strips_sensitive_fields():
    record = build_student_record(
        email="student@example.com",
        package_ids=["galaxies"],
        max_papers_per_week=5,
    )

    public = public_record(record)

    assert "password_hash" not in public
    assert "password_salt" not in public


def test_public_record_handles_legacy_password_fields():
    """Old records with password fields are loaded gracefully."""
    legacy = {
        "email": "student@example.com",
        "package_ids": ["galaxies"],
        "max_papers_per_week": 5,
        "active": True,
        "password_salt": "aabb",
        "password_hash": "scrypt$n=65536,r=8,p=1$deadbeef",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    public = public_record(legacy)
    assert public["email"] == "student@example.com"
    assert "password_salt" not in public
    assert "password_hash" not in public


def test_normalise_public_subscription_clamps_and_validates():
    public = normalise_public_subscription(
        {
            "email": " Student@Example.com ",
            "package_ids": ["stars", "stars", "galaxies"],
            "max_papers_per_week": 99,
            "active": 0,
        }
    )

    assert public == {
        "email": "student@example.com",
        "package_ids": ["stars", "galaxies"],
        "max_papers_per_week": 20,
        "active": False,
    }


def test_build_au_student_config_treats_au_astronomy_as_implicit_baseline():
    config = build_au_student_config(
        "Student Example",
        "student@example.com",
        ["au_astronomy", "galaxies", "cosmology"],
        "simple_and_important",
    )

    assert config["student_tracks"] == ["AU Astronomy", "Galaxies", "Cosmology"]
    assert config["categories"] == ["astro-ph.GA", "astro-ph.CO"]
    assert "galaxies, and cosmology" in config["research_context"].lower()


def test_build_au_student_manage_url_prefills_student_subscription_page():
    url = build_au_student_manage_url(
        "student@example.com",
        ["galaxies", "cosmology"],
        "biggest_only",
        "https://example.com/api/students",
    )

    assert "email=student%40example.com" in url
    assert "packages=galaxies%2Ccosmology" in url
    assert "max_papers=4" in url


def test_build_au_student_subscription_preview_has_no_config_yaml_fields():
    preview = build_au_student_subscription_preview(
        "Student Example",
        "student@example.com",
        ["stars"],
        "simple_and_important",
    )

    assert preview == {
        "student_name": "Student Example",
        "email": "student@example.com",
        "student_tracks": ["AU Astronomy", "Stars"],
        "max_papers_per_week": 6,
        "weekly_style": "Simple + important",
    }


def test_footer_uses_student_manage_links_when_present():
    footer = _render_footer(
        {
            "digest_name": "AU Astronomy Student Weekly",
            "institution": "Aarhus University",
            "department": "Physics and Astronomy",
            "tagline": "",
            "github_repo": "",
            "subscription_manage_url": "https://example.com/manage?email=student@example.com",
            "subscription_unsubscribe_url": "https://example.com/manage?email=student@example.com&mode=unsubscribe",
        },
        "gemini",
    )

    assert "Change settings" in footer
    assert "Change packages" in footer
    assert "Manage subscription" in footer
    assert "Unsubscribe" in footer


def test_student_digest_preview_writes_html(tmp_path, monkeypatch):
    subscriptions = [
        {
            "email": "student@example.com",
            "package_ids": ["exoplanets"],
            "max_papers_per_week": 2,
            "active": True,
        },
        {
            "email": "inactive@example.com",
            "package_ids": ["stars"],
            "max_papers_per_week": 2,
            "active": False,
        },
    ]
    papers = [
        make_paper(
            id="student-paper",
            matched_keywords=["exoplanet"],
            relevance_score=8,
        )
    ]
    sent = []

    monkeypatch.setattr(sd, "fetch_student_subscriptions", lambda: subscriptions)
    monkeypatch.setattr(sd, "fetch_arxiv_papers", lambda config: papers)
    monkeypatch.setattr(sd, "ingest_feedback_from_github", lambda config: {})
    monkeypatch.setattr(sd, "apply_feedback_bias", lambda papers, feedback_stats: None)
    monkeypatch.setattr(sd, "pre_filter", lambda papers: papers)
    monkeypatch.setattr(sd, "analyse_papers", lambda papers, config: (papers, "keywords"))
    monkeypatch.setattr(
        sd,
        "render_html",
        lambda papers, colleague_papers, config, date_str, own_papers, scoring_method: (
            f"{config['recipient_email']}:{len(papers)}"
        ),
    )
    monkeypatch.setattr(
        sd,
        "send_email",
        lambda html, paper_count, date_str, config, papers=None: sent.append(config["recipient_email"]) or True,
    )

    exit_code = sd.main(["--preview", "--preview-dir", str(tmp_path), "--recipient", "student@example.com"])

    assert exit_code == 0
    assert sent == []
    preview_path = tmp_path / "student_example.com.html"
    assert preview_path.read_text(encoding="utf-8") == "student@example.com:1"


def test_fetch_student_subscriptions_skips_invalid_records(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return (
                b'{"subscriptions": ['
                b'{"email": "ok@example.com", "package_ids": ["stars"], "max_papers_per_week": 5, "active": true},'
                b'{"email": "", "package_ids": [], "max_papers_per_week": 3, "active": true}'
                b']}'
            )

    monkeypatch.setenv("STUDENT_ADMIN_TOKEN", "secret-token")
    monkeypatch.setattr(sd.urllib.request, "urlopen", lambda request, timeout=30: FakeResponse())

    subscriptions = sd.fetch_student_subscriptions()

    assert subscriptions == [
        {
            "email": "ok@example.com",
            "package_ids": ["stars"],
            "max_papers_per_week": 5,
            "active": True,
        }
    ]


def test_student_digest_continues_after_send_failure(monkeypatch):
    subscriptions = [
        {
            "email": "first@example.com",
            "package_ids": ["exoplanets"],
            "max_papers_per_week": 2,
            "active": True,
        },
        {
            "email": "second@example.com",
            "package_ids": ["exoplanets"],
            "max_papers_per_week": 2,
            "active": True,
        },
    ]
    papers = [
        make_paper(
            id="student-paper",
            matched_keywords=["exoplanet"],
            relevance_score=8,
        )
    ]
    attempts = []

    monkeypatch.setattr(sd, "fetch_student_subscriptions", lambda: subscriptions)
    monkeypatch.setattr(sd, "fetch_arxiv_papers", lambda config: papers)
    monkeypatch.setattr(sd, "ingest_feedback_from_github", lambda config: {})
    monkeypatch.setattr(sd, "apply_feedback_bias", lambda papers, feedback_stats: None)
    monkeypatch.setattr(sd, "pre_filter", lambda papers: papers)
    monkeypatch.setattr(sd, "analyse_papers", lambda papers, config: (papers, "keywords"))
    monkeypatch.setattr(
        sd,
        "render_html",
        lambda papers, colleague_papers, config, date_str, own_papers, scoring_method: config["recipient_email"],
    )

    def fake_send_email(html, paper_count, date_str, config, papers=None):
        attempts.append(config["recipient_email"])
        return config["recipient_email"] != "first@example.com"

    monkeypatch.setattr(sd, "send_email", fake_send_email)

    exit_code = sd.main([])

    assert exit_code == 1
    assert attempts == ["first@example.com", "second@example.com"]


def test_student_digest_exits_on_registry_auth_error(monkeypatch):
    """HTTP 401 from the student registry should exit with code 1 and a clear message."""
    def raise_401():
        raise urllib.error.HTTPError(
            url="https://example.com/api/students",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=None,
        )

    monkeypatch.setattr(sd, "fetch_student_subscriptions", raise_401)
    exit_code = sd.main([])
    assert exit_code == 1


def test_student_digest_exits_on_registry_forbidden(monkeypatch):
    """HTTP 403 from the student registry should exit with code 1."""
    def raise_403():
        raise urllib.error.HTTPError(
            url="https://example.com/api/students",
            code=403,
            msg="Forbidden",
            hdrs={},
            fp=None,
        )

    monkeypatch.setattr(sd, "fetch_student_subscriptions", raise_403)
    exit_code = sd.main([])
    assert exit_code == 1


def test_student_digest_exits_on_registry_network_error(monkeypatch):
    """URLError (network failure) should exit with code 1."""
    def raise_url_error():
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(sd, "fetch_student_subscriptions", raise_url_error)
    exit_code = sd.main([])
    assert exit_code == 1


def test_student_digest_exits_on_missing_admin_token(monkeypatch):
    """RuntimeError from missing STUDENT_ADMIN_TOKEN should exit with code 1."""
    def raise_runtime():
        raise RuntimeError("STUDENT_ADMIN_TOKEN is required for student digests.")

    monkeypatch.setattr(sd, "fetch_student_subscriptions", raise_runtime)
    exit_code = sd.main([])
    assert exit_code == 1


def test_student_digest_continues_after_unexpected_per_student_error(monkeypatch):
    """An unexpected error for one student should not crash the batch."""
    subscriptions = [
        {
            "email": "crash@example.com",
            "package_ids": ["exoplanets"],
            "max_papers_per_week": 2,
            "active": True,
        },
        {
            "email": "ok@example.com",
            "package_ids": ["exoplanets"],
            "max_papers_per_week": 2,
            "active": True,
        },
    ]
    papers = [
        make_paper(id="p1", matched_keywords=["exoplanet"], relevance_score=8)
    ]
    sent = []
    call_count = [0]

    monkeypatch.setattr(sd, "fetch_student_subscriptions", lambda: subscriptions)
    monkeypatch.setattr(sd, "fetch_arxiv_papers", lambda config: papers)
    monkeypatch.setattr(sd, "ingest_feedback_from_github", lambda config: {})
    monkeypatch.setattr(sd, "apply_feedback_bias", lambda papers, feedback_stats: None)
    monkeypatch.setattr(sd, "pre_filter", lambda papers: papers)
    monkeypatch.setattr(sd, "analyse_papers", lambda papers, config: (papers, "keywords"))

    def fake_render(papers, colleague_papers, config, date_str, own_papers, scoring_method):
        call_count[0] += 1
        if call_count[0] == 1:
            raise KeyError("simulated crash")
        return "html"

    monkeypatch.setattr(sd, "render_html", fake_render)
    monkeypatch.setattr(
        sd, "send_email",
        lambda html, paper_count, date_str, config, papers=None: sent.append(config["recipient_email"]) or True,
    )

    exit_code = sd.main([])

    assert exit_code == 1  # one failure means exit code 1
    assert sent == ["ok@example.com"]
