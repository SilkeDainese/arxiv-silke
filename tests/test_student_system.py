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
        "plain_summary": "A test summary about exoplanets.",
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
    assert "Change categories" in footer
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
        def raise_for_status(self):
            pass
        def json(self):
            return {"subscriptions": [
                {"email": "ok@example.com", "package_ids": ["stars"], "max_papers_per_week": 5, "active": True},
                {"email": "", "package_ids": [], "max_papers_per_week": 3, "active": True}
            ]}

    monkeypatch.setenv("STUDENT_ADMIN_TOKEN", "secret-token")
    monkeypatch.setattr(sd.requests, "post", lambda *args, **kwargs: FakeResponse())

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
    monkeypatch.setattr(sd, "analyse_papers", lambda papers, config: (papers, "claude"))
    monkeypatch.setattr(
        sd,
        "render_html",
        lambda papers, colleague_papers, config, date_str, own_papers, scoring_method: config["recipient_email"],
    )

    def fake_send_email(html, paper_count, date_str, config, papers=None):
        attempts.append(config["recipient_email"])
        return config["recipient_email"] != "first@example.com"

    monkeypatch.setattr(sd, "send_email", fake_send_email)
    monkeypatch.setattr(sd, "_send_admin_alert", lambda subject, body: None)

    exit_code = sd.main([])

    assert exit_code == 1
    assert attempts == ["first@example.com", "second@example.com"]


def test_student_digest_exits_on_registry_auth_error(monkeypatch):
    """HTTP 401 from the student registry should exit with code 1 and a clear message."""
    def raise_401():
        exc = sd.requests.exceptions.HTTPError("Unauthorized")
        exc.response = type("Response", (), {"status_code": 401})()
        raise exc

    monkeypatch.setattr(sd, "fetch_student_subscriptions", raise_401)
    exit_code = sd.main([])
    assert exit_code == 1


def test_student_digest_exits_on_registry_forbidden(monkeypatch):
    """HTTP 403 from the student registry should exit with code 1."""
    def raise_403():
        exc = sd.requests.exceptions.HTTPError("Forbidden")
        exc.response = type("Response", (), {"status_code": 403})()
        raise exc

    monkeypatch.setattr(sd, "fetch_student_subscriptions", raise_403)
    exit_code = sd.main([])
    assert exit_code == 1


def test_student_digest_exits_on_registry_network_error(monkeypatch):
    """URLError (network failure) should exit with code 1."""
    def raise_url_error():
        raise sd.requests.exceptions.RequestException("Connection refused")

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
    monkeypatch.setattr(sd, "analyse_papers", lambda papers, config: (papers, "claude"))

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
    monkeypatch.setattr(sd, "_send_admin_alert", lambda subject, body: None)

    exit_code = sd.main([])

    assert exit_code == 1  # one failure means exit code 1
    assert sent == ["ok@example.com"]


# ─────── Failure state handling ───────────────────────────────

def test_keyword_scoring_aborts_student_digest_and_alerts(monkeypatch):
    """Keyword-only scoring should abort the student digest and send an admin alert."""
    subscriptions = [
        {"email": "student@example.com", "package_ids": ["exoplanets"],
         "max_papers_per_week": 2, "active": True},
    ]
    papers = [make_paper(id="p1", matched_keywords=["exoplanet"], relevance_score=5)]
    alerts = []

    monkeypatch.setattr(sd, "fetch_student_subscriptions", lambda: subscriptions)
    monkeypatch.setattr(sd, "fetch_arxiv_papers", lambda config: papers)
    monkeypatch.setattr(sd, "ingest_feedback_from_github", lambda config: {})
    monkeypatch.setattr(sd, "apply_feedback_bias", lambda papers, fb: None)
    monkeypatch.setattr(sd, "pre_filter", lambda papers: papers)
    monkeypatch.setattr(sd, "analyse_papers", lambda papers, config: (papers, "keywords"))
    monkeypatch.setattr(sd, "_send_admin_alert", lambda subject, body: alerts.append(subject))

    exit_code = sd.main([])

    assert exit_code == 1
    assert len(alerts) == 1
    assert "AI scoring failed" in alerts[0]


def test_keyword_scoring_allowed_in_preview_mode(monkeypatch):
    """Preview mode should not abort on keyword-only scoring."""
    subscriptions = [
        {"email": "student@example.com", "package_ids": ["exoplanets"],
         "max_papers_per_week": 2, "active": True},
    ]
    papers = [make_paper(id="p1", matched_keywords=["exoplanet"], relevance_score=5)]

    monkeypatch.setattr(sd, "fetch_student_subscriptions", lambda: subscriptions)
    monkeypatch.setattr(sd, "fetch_arxiv_papers", lambda config: papers)
    monkeypatch.setattr(sd, "ingest_feedback_from_github", lambda config: {})
    monkeypatch.setattr(sd, "apply_feedback_bias", lambda papers, fb: None)
    monkeypatch.setattr(sd, "pre_filter", lambda papers: papers)
    monkeypatch.setattr(sd, "analyse_papers", lambda papers, config: (papers, "keywords"))

    exit_code = sd.main(["--preview", "--preview-dir", "/tmp/test_previews"])

    assert exit_code == 0


def test_arxiv_fetch_failure_alerts_admin(monkeypatch):
    """Empty arXiv fetch should send an admin alert."""
    subscriptions = [
        {"email": "student@example.com", "package_ids": ["exoplanets"],
         "max_papers_per_week": 2, "active": True},
    ]
    alerts = []

    monkeypatch.setattr(sd, "fetch_student_subscriptions", lambda: subscriptions)
    monkeypatch.setattr(sd, "fetch_arxiv_papers", lambda config: [])
    monkeypatch.setattr(sd, "_send_admin_alert", lambda subject, body: alerts.append(subject))

    exit_code = sd.main([])

    assert exit_code == 1
    assert len(alerts) == 1
    assert "arXiv fetch failed" in alerts[0]


# ─────── Phase 1 UX redesign: student footer ─────────────────

def test_student_footer_shows_silke_attribution():
    """Student footer must say 'Made by Silke Dainese', not 'Aarhus University'."""
    from digest import _render_student_footer
    config = {
        "digest_name": "AU Astronomy Student Weekly",
        "institution": "Aarhus University",
        "department": "Physics and Astronomy",
        "tagline": "",
        "github_repo": "",
        "subscription_manage_url": "https://example.com/manage?email=s@e.com",
        "subscription_unsubscribe_url": "https://example.com/manage?email=s@e.com&mode=unsubscribe",
    }
    footer = _render_student_footer(config, "gemini")
    assert "Made by" in footer and "Silke Dainese" in footer, "Student footer must credit Silke"
    # "Aarhus University" may appear in the disclaimer ("not affiliated with") but
    # must NOT appear as institutional branding (the location/header line).
    import re
    # Strip the disclaimer sentence to check that AU doesn't appear elsewhere
    without_disclaimer = re.sub(r"not affiliated with Aarhus University", "", footer)
    assert "Aarhus University" not in without_disclaimer, (
        "Student footer must not use Aarhus University as institutional branding"
    )


# ─────── Phase 2 UX redesign: email card visual overhaul ──────

def test_student_card_has_no_keyword_pills():
    """Student paper card must not render keyword pills (border-radius spans for matched_keywords)."""
    from digest import _render_student_paper_card

    paper = make_paper(
        matched_keywords=["exoplanet", "transit", "habitable"],
        student_package_ids=["exoplanets"],
    )
    html = _render_student_paper_card(paper)
    # Old design had keyword spans with border-radius:3px pills
    for kw in ["exoplanet", "transit", "habitable"]:
        # The keyword text must not appear inside a styled pill/tag span
        assert f">{kw}</span>" not in html, (
            f"Keyword '{kw}' should not appear as a pill/tag in the student card"
        )


def test_student_card_has_category_label():
    """Student paper card must show category as a text label, not a green pill badge."""
    from digest import _render_student_paper_card

    paper = make_paper(
        category="astro-ph.SR",
        student_package_ids=["stars"],
    )
    html = _render_student_paper_card(paper)
    assert "Stars" in html, "Card must show friendly category name 'Stars'"
    assert "astro-ph.SR" in html, "Card must show arXiv category code"
    # Must NOT be a green pill badge (old design used PINE background)
    assert "border-radius:4px" not in html, "Category must not be a pill badge"


def test_student_header_uses_warm_white():
    """Student header text must use WARM_WHITE (#FFFDF8), not plain white."""
    from digest import _render_student_header

    html = _render_student_header([make_paper()], "2026-03-22")
    assert "#FFFDF8" in html, "Student header must use WARM_WHITE (#FFFDF8)"


# ─────── detect_delights tests ──────────────────────────────


def test_detect_delights_au_affiliation():
    """Paper with AU-affiliated PhD student author gets AU delight."""
    from digest import detect_delights

    paper = make_paper(
        is_au_researcher=True,
        au_researcher_authors=["Jane Doe"],
        author_affiliations={"Jane Doe": ["PhD student, Aarhus University"]},
        abstract="We study exoplanets.",
    )
    detect_delights([paper])
    assert any("AU" in d for d in paper["delights"]), "AU affiliation delight expected"
    assert any("PhD" in d for d in paper["delights"]), "PhD role should appear in delight"


def test_detect_delights_au_telescope():
    """Paper mentioning SONG telescope gets AU-local delight."""
    from digest import detect_delights

    paper = make_paper(
        abstract="We present new SONG telescope observations of stellar oscillations.",
    )
    detect_delights([paper])
    assert any("SONG" in d for d in paper["delights"])


def test_detect_delights_max_per_email():
    """Delights are capped at 2 across the entire email, not per paper."""
    from digest import detect_delights

    papers = [
        make_paper(abstract="SONG telescope data."),
        make_paper(abstract="Nordic Optical Telescope observations."),
        make_paper(abstract="Ole Rømer Observatory data."),
    ]
    detect_delights(papers)
    total = sum(len(p["delights"]) for p in papers)
    assert total <= 2


def test_detect_delights_generic_telescopes_not_included():
    """JWST, TESS, Hubble etc. are NOT delights — too common, not AU-local."""
    from digest import detect_delights

    paper = make_paper(
        abstract="The James Webb Space Telescope (JWST) and TESS observed the target.",
    )
    detect_delights([paper])
    assert paper["delights"] == []


def test_detect_delights_empty_when_no_match():
    """Paper with no keyword matches and no AU affiliation gets empty delights."""
    from digest import detect_delights

    paper = make_paper(abstract="A study of fluid dynamics in pipes.")
    detect_delights([paper])
    assert paper["delights"] == []
