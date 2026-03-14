import student_digest as sd
from digest import _render_footer
from student_digest import annotate_student_packages, make_student_digest_config, select_student_papers
from student_registry import (
    build_student_record,
    normalise_package_ids,
    public_record,
    verify_password,
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


def test_build_student_record_hashes_and_verifies_password():
    record = build_student_record(
        email="Student@Example.com",
        password="secret-password",
        package_ids=["exoplanets", "galaxies"],
        max_papers_per_week=9,
    )

    assert record["email"] == "student@example.com"
    assert record["package_ids"] == ["exoplanets", "galaxies"]
    assert record["max_papers_per_week"] == 9
    assert verify_password("secret-password", record["password_salt"], record["password_hash"])
    assert not verify_password("wrong-password", record["password_salt"], record["password_hash"])


def test_build_student_record_requires_correct_password_for_updates():
    original = build_student_record(
        email="student@example.com",
        password="secret-password",
        package_ids=["exoplanets"],
        max_papers_per_week=6,
    )

    updated = build_student_record(
        email="student@example.com",
        password="secret-password",
        package_ids=["stars", "galaxies"],
        max_papers_per_week=4,
        existing=original,
    )

    assert updated["package_ids"] == ["stars", "galaxies"]
    assert updated["max_papers_per_week"] == 4


def test_build_student_record_can_rotate_password():
    original = build_student_record(
        email="student@example.com",
        password="old-password",
        package_ids=["exoplanets"],
        max_papers_per_week=6,
    )

    updated = build_student_record(
        email="student@example.com",
        password="old-password",
        new_password="new-password",
        package_ids=["stars"],
        max_papers_per_week=5,
        existing=original,
    )

    assert verify_password("new-password", updated["password_salt"], updated["password_hash"])
    assert not verify_password("old-password", updated["password_salt"], updated["password_hash"])


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
    assert set(papers[1]["student_package_ids"]) == {"stars"}
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
    assert "Planets + exoplanets" in config["tagline"]
    assert "Galaxies" in config["tagline"]


def test_public_record_strips_sensitive_fields():
    record = build_student_record(
        email="student@example.com",
        password="secret-password",
        package_ids=["galaxies"],
        max_papers_per_week=5,
    )

    public = public_record(record)

    assert "password_hash" not in public
    assert "password_salt" not in public


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
