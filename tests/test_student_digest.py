"""
tests/test_student_digest.py — Edge-case and guard tests for student_digest.py.

Covers failure modes identified in Round 4 QA:
  - 0-paper guard: arXiv down → no email sent, exit non-zero
  - Package ordering: category match must outrank keyword-only match
"""

from __future__ import annotations

from unittest.mock import call, patch, MagicMock

import pytest

import student_digest as sd


# ─────────────────────────────────────────────────────────────
#  main() — 0-paper early exit (all arXiv fetches failed)
# ─────────────────────────────────────────────────────────────


class TestStudentZeroPaperGuard:
    """When fetch_arxiv_papers returns [], main() must return 1 and never call send_email."""

    _FAKE_SUBSCRIPTION = {
        "email": "student@example.com",
        "active": True,
        "package_ids": ["exoplanets"],
        "max_papers_per_week": 6,
        "created_at": "2025-01-01",
        "manage_url": "https://example.com",
    }

    def test_zero_papers_skips_all_students_and_exits_nonzero(self):
        """When arXiv returns no papers, main() returns 1 and never sends email."""
        with (
            patch.object(sd, "fetch_student_subscriptions", return_value=[self._FAKE_SUBSCRIPTION]),
            patch.object(sd, "fetch_arxiv_papers", return_value=[]),
            patch.object(sd, "send_email") as mock_send,
            patch.object(sd, "ingest_feedback_from_github", return_value={}),
        ):
            result = sd.main(["--preview"])
        assert result == 1, "Expected exit code 1 when no papers fetched"
        mock_send.assert_not_called()


# ─────────────────────────────────────────────────────────────
#  annotate_student_packages — category match must take priority
# ─────────────────────────────────────────────────────────────


class TestAnnotateStudentPackagesOrdering:
    """Category matches must rank before keyword-only matches in student_package_ids.

    Regression: galaxy papers that also mention "stellar" were being labelled
    "Stars" because AVAILABLE_STUDENT_PACKAGES is iterated in fixed order and
    "stars" appears before "galaxies".
    """

    def test_galaxy_paper_with_stellar_keyword_has_galaxies_first(self):
        """A paper in astro-ph.GA that also matches 'stellar' must list 'galaxies' first."""
        # This paper lives in the Galaxies category but its abstract mentions "stellar"
        # so it matches the "stars" keyword set as well.
        paper = {
            "id": "2501.00001",
            "title": "Stellar populations in nearby galaxies",
            "category": "astro-ph.GA",
            "matched_keywords": ["stellar", "galaxy"],
        }
        sd.annotate_student_packages([paper])

        pkg_ids = paper["student_package_ids"]
        assert "galaxies" in pkg_ids, "Expected 'galaxies' to be in matched packages"
        assert pkg_ids[0] == "galaxies", (
            f"Expected 'galaxies' as first package (category match) but got '{pkg_ids[0]}'"
        )


# ─────────────────────────────────────────────────────────────
#  --send-preview flag
# ─────────────────────────────────────────────────────────────


class TestSendPreviewFlag:
    """Verify --send-preview sends one email to RECIPIENT_EMAIL with [PREVIEW] prefix."""

    _FAKE_SUBSCRIPTION = {
        "email": "student@example.com",
        "active": True,
        "package_ids": ["exoplanets"],
        "max_papers_per_week": 6,
        "created_at": "2025-01-01",
        "manage_url": "https://example.com",
    }

    _FAKE_PAPER = {
        "id": "2501.99999",
        "title": "A fake paper about exoplanets",
        "category": "astro-ph.EP",
        "abstract": "Exoplanet detection methods.",
        "authors": ["A. Test"],
        "published": "2025-01-01T00:00:00Z",
        "matched_keywords": ["exoplanet"],
        "relevance_score": 8,
        "student_package_ids": ["exoplanets"],
        "student_au_priority": 0,
        "expert_net": 0,
    }

    def _run_send_preview(self, env_overrides=None):
        """Helper: run main(["--send-preview"]) with standard mocks.

        Returns (exit_code, mock_send_email).
        """
        env = {"RECIPIENT_EMAIL": "silke@example.com", "STUDENT_ADMIN_TOKEN": "tok"}
        if env_overrides:
            env.update(env_overrides)
        with (
            patch.dict("os.environ", env, clear=False),
            patch.object(sd, "fetch_student_subscriptions", return_value=[self._FAKE_SUBSCRIPTION]),
            patch.object(sd, "fetch_arxiv_papers", return_value=[self._FAKE_PAPER]),
            patch.object(sd, "ingest_feedback_from_github", return_value={}),
            patch.object(sd, "pre_filter", return_value=[self._FAKE_PAPER]),
            patch.object(sd, "fetch_aggregate_feedback", return_value={}),
            patch.object(sd, "analyse_papers", return_value=([self._FAKE_PAPER], "keyword")),
            patch.object(sd, "annotate_student_packages"),
            patch.object(sd, "detect_au_researchers"),
            patch.object(sd, "detect_delights"),
            patch.object(sd, "render_html", return_value="<html>preview</html>"),
            patch.object(sd, "send_email", return_value=True) as mock_send,
        ):
            result = sd.main(["--send-preview"])
        return result, mock_send

    def test_send_preview_flag_uses_recipient_email(self):
        """--send-preview sends to RECIPIENT_EMAIL, not the student's email."""
        result, mock_send = self._run_send_preview()
        assert result == 0
        mock_send.assert_called_once()
        config_arg = mock_send.call_args[0][3]
        assert config_arg["recipient_email"] == "silke@example.com"

    def test_send_preview_adds_subject_prefix(self):
        """--send-preview passes '[PREVIEW] ' as subject_prefix to send_email."""
        result, mock_send = self._run_send_preview()
        assert result == 0
        mock_send.assert_called_once()
        kwargs = mock_send.call_args
        assert kwargs.kwargs.get("subject_prefix") == "[PREVIEW] " or (
            len(kwargs.args) > 5 and kwargs.args[5] == "[PREVIEW] "
        )

    def test_send_preview_mutually_exclusive_with_preview(self):
        """--send-preview and --preview cannot be used together."""
        with pytest.raises(SystemExit):
            sd.build_parser().parse_args(["--preview", "--send-preview"])


# ─────────────────────────────────────────────────────────────
#  Welcome header — first digest only
# ─────────────────────────────────────────────────────────────


class TestWelcomeHeader:
    """First digest must show a welcome block; subsequent ones must not."""

    def test_make_student_digest_config_sets_show_welcome_when_not_sent(self):
        """Config gets show_welcome=True when welcome_sent is False."""
        base = sd.build_student_base_config()
        subscription = {
            "email": "student@example.com",
            "package_ids": ["exoplanets"],
            "max_papers_per_week": 6,
            "welcome_sent": False,
        }
        config = sd.make_student_digest_config(base, subscription)
        assert config.get("show_welcome") is True

    def test_make_student_digest_config_no_welcome_when_already_sent(self):
        """Config must NOT have show_welcome when welcome_sent is True."""
        base = sd.build_student_base_config()
        subscription = {
            "email": "student@example.com",
            "package_ids": ["exoplanets"],
            "max_papers_per_week": 6,
            "welcome_sent": True,
        }
        config = sd.make_student_digest_config(base, subscription)
        assert not config.get("show_welcome")

    def test_make_student_digest_config_no_welcome_when_field_absent(self):
        """Config must NOT have show_welcome when welcome_sent key is missing (backward compat)."""
        base = sd.build_student_base_config()
        subscription = {
            "email": "student@example.com",
            "package_ids": ["exoplanets"],
            "max_papers_per_week": 6,
        }
        config = sd.make_student_digest_config(base, subscription)
        assert not config.get("show_welcome")

    def test_first_digest_has_welcome_header(self):
        """Rendered HTML with show_welcome=True must contain the welcome heading."""
        from digest import render_html
        config = {
            "digest_name": "AU Astronomy Student Weekly",
            "researcher_name": "Student",
            "research_context": "",
            "institution": "",
            "department": "",
            "tagline": "Your categories: Exoplanets",
            "github_repo": "",
            "recipient_view_mode": "deep_read",
            "subscription_manage_url": "https://example.com/manage",
            "subscription_unsubscribe_url": "https://example.com/unsub",
            "show_welcome": True,
        }
        paper = {
            "id": "2501.00001",
            "title": "Exoplanet atmospheres",
            "abstract": "We study atmospheres.",
            "authors": ["A. Test"],
            "published": "2025-01-01",
            "category": "astro-ph.EP",
            "url": "https://arxiv.org/abs/2501.00001",
            "matched_keywords": ["exoplanet"],
            "relevance_score": 7,
            "student_package_ids": ["exoplanets"],
            "student_au_priority": 0,
            "expert_net": 0,
            "colleague_matches": [],
        }
        html = render_html([paper], [], config, "January 01, 2025", own_papers=[], scoring_method="keyword")
        assert "Welcome to the AU student digest" in html

    def test_second_digest_has_no_welcome_header(self):
        """Rendered HTML without show_welcome must NOT contain the welcome block."""
        from digest import render_html
        config = {
            "digest_name": "AU Astronomy Student Weekly",
            "researcher_name": "Student",
            "research_context": "",
            "institution": "",
            "department": "",
            "tagline": "Your categories: Exoplanets",
            "github_repo": "",
            "recipient_view_mode": "deep_read",
            "subscription_manage_url": "https://example.com/manage",
            "subscription_unsubscribe_url": "https://example.com/unsub",
        }
        paper = {
            "id": "2501.00001",
            "title": "Exoplanet atmospheres",
            "abstract": "We study atmospheres.",
            "authors": ["A. Test"],
            "published": "2025-01-01",
            "category": "astro-ph.EP",
            "url": "https://arxiv.org/abs/2501.00001",
            "matched_keywords": ["exoplanet"],
            "relevance_score": 7,
            "student_package_ids": ["exoplanets"],
            "student_au_priority": 0,
            "expert_net": 0,
            "colleague_matches": [],
        }
        html = render_html([paper], [], config, "January 01, 2025", own_papers=[], scoring_method="keyword")
        assert "Welcome to the AU student digest" not in html


# ─────────────────────────────────────────────────────────────
#  rewrite_summaries_for_students()
# ─────────────────────────────────────────────────────────────


class TestStudentSummaryRewrite:
    """Student summaries should be rewritten to avoid jargon."""

    def _make_papers(self):
        return [
            {
                "title": "Black hole superradiance from ultralight axions",
                "plain_summary": "Black hole superradiance is a powerful probe of ultralight axions.",
                "abstract": "We constrain ultralight axion masses using superradiant instabilities.",
            },
            {
                "title": "TOI-1232 b: A warm Neptune",
                "plain_summary": "TOI-1232 is a G-dwarf with $1.06 M_\\odot$.",
                "abstract": "We report two planets transiting TOI-1232.",
            },
        ]

    def test_rewrite_replaces_plain_summary(self):
        """Successful rewrite replaces plain_summary with student-friendly version."""
        mock_anthropic = MagicMock()
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text='[{"summary": "Spinning black holes can reveal invisible particles."}, {"summary": "Two warm planets found orbiting a Sun-like star."}]')]
        )

        papers = self._make_papers()
        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            sd.rewrite_summaries_for_students(papers, "fake-key")

        assert papers[0]["plain_summary"] == "Spinning black holes can reveal invisible particles."
        assert papers[1]["plain_summary"] == "Two warm planets found orbiting a Sun-like star."

    def test_rewrite_keeps_original_on_failure(self):
        """If the API call fails, original summaries are preserved."""
        mock_anthropic = MagicMock()
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.side_effect = Exception("API error")

        papers = self._make_papers()
        original_0 = papers[0]["plain_summary"]
        original_1 = papers[1]["plain_summary"]
        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            sd.rewrite_summaries_for_students(papers, "fake-key")

        assert papers[0]["plain_summary"] == original_0
        assert papers[1]["plain_summary"] == original_1

    def test_rewrite_skipped_without_api_key(self):
        """No API key means no rewrite — originals preserved."""
        papers = self._make_papers()
        original = papers[0]["plain_summary"]
        sd.rewrite_summaries_for_students(papers, "")
        assert papers[0]["plain_summary"] == original


# ─────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────

from datetime import datetime, timezone
from typing import Any


def make_paper(**overrides: Any) -> dict[str, Any]:
    """Minimal valid paper dict for testing; override any field as needed."""
    defaults = {
        "id": "2501.00001",
        "title": "Test Paper",
        "authors": ["A. Author"],
        "category": "astro-ph.SR",
        "url": "https://arxiv.org/abs/2501.00001",
        "abstract": "A test abstract about stars.",
        "published": datetime.now(timezone.utc).isoformat(),
        "matched_keywords": ["stellar rotation"],
        "keyword_hits": 5,
        "relevance_score": 6,
        "feedback_bias": 0,
        "student_package_ids": ["stars"],
        "student_au_priority": 0,
        "expert_net": 0,
        "journal_ref": "",
        "plain_summary": "A test summary about stars.",
    }
    defaults.update(overrides)
    return defaults


# ─────────────────────────────────────────────────────────────
#  _is_astronomy_relevant() guard
# ─────────────────────────────────────────────────────────────


class TestAstronomyGuard:
    """Unit tests for the _is_astronomy_relevant() gate."""

    def test_astronomy_guard_blocks_non_astro_ml_only(self):
        """A stat.ML paper whose only package is methods_ml must be blocked."""
        paper = make_paper(
            category="stat.ML",
            student_package_ids=["methods_ml"],
            colleague_matches=[],
            known_authors=[],
        )
        assert sd._is_astronomy_relevant(paper) is False

    def test_astronomy_guard_allows_astro_ph(self):
        """Any astro-ph.* paper always passes the guard regardless of packages."""
        paper = make_paper(
            category="astro-ph.SR",
            student_package_ids=["methods_ml"],  # even if only methods_ml matched
        )
        assert sd._is_astronomy_relevant(paper) is True

    def test_astronomy_guard_allows_non_astro_with_astro_keywords(self):
        """A stat.ML paper that also matched the 'stars' package passes the guard."""
        paper = make_paper(
            category="stat.ML",
            student_package_ids=["methods_ml", "stars"],
            colleague_matches=[],
            known_authors=[],
        )
        assert sd._is_astronomy_relevant(paper) is True


# ─────────────────────────────────────────────────────────────
#  ML paper cap
# ─────────────────────────────────────────────────────────────


class TestMLCap:
    """select_student_papers must never include more than 2 methods/ML papers."""

    def _make_ml_paper(self, paper_id: str) -> dict[str, Any]:
        return make_paper(
            id=paper_id,
            category="stat.ML",
            student_package_ids=["methods_ml"],
            relevance_score=5,
        )

    def _make_astro_paper(self, paper_id: str) -> dict[str, Any]:
        return make_paper(
            id=paper_id,
            category="astro-ph.SR",
            student_package_ids=["stars"],
            relevance_score=5,
        )

    def test_ml_cap_limits_to_two(self):
        """With 5 ML-only and 5 astro papers, only 2 ML papers make it into the result."""
        papers = (
            [self._make_ml_paper(f"ml-{i}") for i in range(5)]
            + [self._make_astro_paper(f"astro-{i}") for i in range(5)]
        )
        selected = sd.select_student_papers(papers, ["methods_ml", "stars"], max_papers_per_week=20)
        ml_papers = [p for p in selected if p["category"] == "stat.ML"]
        assert len(ml_papers) <= 2, (
            f"Expected at most 2 ML papers, got {len(ml_papers)}"
        )


# ─────────────────────────────────────────────────────────────
#  All astro-ph papers are always eligible
# ─────────────────────────────────────────────────────────────


class TestAllAstroPhIncluded:
    """Astro-ph papers must appear in any student's digest regardless of package choice."""

    def test_all_astro_papers_included_regardless_of_package(self):
        """Even if a student selected only 'stars', astro-ph.GA papers are still included."""
        stars_paper = make_paper(
            id="stars-001",
            category="astro-ph.SR",
            student_package_ids=["stars"],
            relevance_score=7,
        )
        galaxies_paper = make_paper(
            id="galaxies-001",
            category="astro-ph.GA",
            student_package_ids=["galaxies"],
            relevance_score=7,
        )
        papers = [stars_paper, galaxies_paper]

        # Student only subscribed to "stars"
        selected = sd.select_student_papers(papers, ["stars"], max_papers_per_week=10)
        selected_ids = {p["id"] for p in selected}

        assert "galaxies-001" in selected_ids, (
            "astro-ph.GA paper should be included even when student only chose 'stars'"
        )


# ─────────────────────────────────────────────────────────────
#  Prestige journal detection
# ─────────────────────────────────────────────────────────────


class TestPrestigeDetection:
    """detect_prestige() must flag papers from high-impact journals."""

    def test_prestige_journal_detected(self):
        """A paper with journal_ref containing a prestige journal name gets prestige_journal set.

        Uses 'Physical Review Letters' to avoid the substring ambiguity between
        'nature' (matched first in the dict) and 'nature astronomy'.
        """
        paper = make_paper(journal_ref="Physical Review Letters, 134, 2025")
        sd.detect_prestige([paper])
        assert paper.get("prestige_journal") == "PRL"

    def test_prestige_boosts_ranking(self):
        """A prestige paper ranks above an equal-score non-prestige paper."""
        prestige_paper = make_paper(
            id="prestige-001",
            category="astro-ph.GA",
            student_package_ids=["galaxies"],
            relevance_score=5,
            prestige_journal="Nature Astronomy",
        )
        plain_paper = make_paper(
            id="plain-001",
            category="astro-ph.GA",
            student_package_ids=["galaxies"],
            relevance_score=5,
            journal_ref="",
        )
        selected = sd.select_student_papers(
            [plain_paper, prestige_paper], ["galaxies"], max_papers_per_week=10
        )
        assert selected[0]["id"] == "prestige-001", (
            "Prestige paper should rank first when all other scores are equal"
        )


# ─────────────────────────────────────────────────────────────
#  AU priority ranking
# ─────────────────────────────────────────────────────────────


class TestAUPriorityRanking:
    """AU colleague papers must outrank all others."""

    def test_au_priority_ranks_highest(self):
        """A paper matching an AU colleague ranks above a higher-relevance non-AU paper.

        The sort key is (au_priority + has_prestige, ...). An AU paper with
        student_au_priority=1 scores at least as high as any prestige paper
        on that first key. To test AU-beats-everyone unambiguously we compare
        against a plain high-relevance paper with no AU or prestige boost.
        """
        au_paper = make_paper(
            id="au-001",
            category="astro-ph.SR",
            student_package_ids=["stars"],
            relevance_score=5,
            student_au_priority=1,
        )
        high_score_paper = make_paper(
            id="high-001",
            category="astro-ph.SR",
            student_package_ids=["stars"],
            relevance_score=10,
            student_au_priority=0,
        )
        # au_paper: first-key = 1+0 = 1; high_score_paper: 0+0 = 0 → AU ranks first
        selected = sd.select_student_papers(
            [high_score_paper, au_paper],
            ["stars"],
            max_papers_per_week=10,
        )
        assert selected[0]["id"] == "au-001", (
            "AU priority paper should rank first above a plain high-relevance paper"
        )


# ─────────────────────────────────────────────────────────────
#  Pre-send validation
# ─────────────────────────────────────────────────────────────


class TestPreSendValidation:
    """main() must return 1 and refuse to send if pre-send validation fails."""

    _BASE_ENV = {"STUDENT_ADMIN_TOKEN": "tok"}

    _ASTRO_PAPER = make_paper(
        id="astro-valid-001",
        category="astro-ph.SR",
        student_package_ids=["stars"],
        relevance_score=7,
        expert_net=0,
        student_au_priority=0,
    )

    def _run_main(self, subscriptions, papers, env_overrides=None):
        """Run main() (no flags) with the given subs and papers. Returns exit code."""
        env = dict(self._BASE_ENV)
        if env_overrides:
            env.update(env_overrides)

        with (
            patch.dict("os.environ", env, clear=False),
            patch.object(sd, "fetch_student_subscriptions", return_value=subscriptions),
            patch.object(sd, "fetch_arxiv_papers", return_value=papers),
            patch.object(sd, "ingest_feedback_from_github", return_value={}),
            patch.object(sd, "pre_filter", return_value=papers),
            patch.object(sd, "fetch_aggregate_feedback", return_value={}),
            patch.object(sd, "analyse_papers", return_value=(papers, "keyword")),
            patch.object(sd, "annotate_student_packages"),
            patch.object(sd, "detect_au_researchers"),
            patch.object(sd, "detect_delights"),
            patch.object(sd, "detect_prestige"),
            patch.object(sd, "render_html", return_value="<html></html>"),
            patch.object(sd, "send_email", return_value=True),
        ):
            return sd.main([])

    def test_no_empty_digests_in_validation(self):
        """Validation fails and returns 1 when a student would receive 0 papers.

        We mock select_student_papers to return [] only for the validation probe
        call, simulating a student whose preferences would produce an empty digest.
        """
        student_sub = {
            "email": "empty-student@example.com",
            "active": True,
            "package_ids": ["stars"],
            "max_papers_per_week": 6,
            "created_at": "2025-01-01",
            "manage_url": "https://example.com",
        }
        env = dict(self._BASE_ENV)
        with (
            patch.dict("os.environ", env, clear=False),
            patch.object(sd, "fetch_student_subscriptions", return_value=[student_sub]),
            patch.object(sd, "fetch_arxiv_papers", return_value=[self._ASTRO_PAPER]),
            patch.object(sd, "ingest_feedback_from_github", return_value={}),
            patch.object(sd, "pre_filter", return_value=[self._ASTRO_PAPER]),
            patch.object(sd, "fetch_aggregate_feedback", return_value={}),
            patch.object(sd, "analyse_papers", return_value=([self._ASTRO_PAPER], "keyword")),
            patch.object(sd, "annotate_student_packages"),
            patch.object(sd, "detect_au_researchers"),
            patch.object(sd, "detect_delights"),
            patch.object(sd, "detect_prestige"),
            patch.object(sd, "render_html", return_value="<html></html>"),
            patch.object(sd, "send_email", return_value=True),
            # Force the validation probe to see an empty selection
            patch.object(sd, "select_student_papers", return_value=[]),
        ):
            result = sd.main([])
        assert result == 1, "Expected exit code 1 when a student gets 0 papers in validation"

    def test_duplicate_email_caught_in_validation(self):
        """Validation fails and returns 1 when two subscriptions share the same email."""
        sub = {
            "email": "dup@example.com",
            "active": True,
            "package_ids": ["stars"],
            "max_papers_per_week": 6,
            "created_at": "2025-01-01",
            "manage_url": "https://example.com",
        }
        # Two identical subscriptions for the same email
        result = self._run_main([sub, sub], [self._ASTRO_PAPER])
        assert result == 1, "Expected exit code 1 when duplicate emails are detected"

    def test_missing_summary_caught_in_validation(self):
        """Validation fails when a paper has no plain_summary."""
        sub = {
            "email": "student@example.com",
            "active": True,
            "package_ids": ["stars"],
            "max_papers_per_week": 6,
            "created_at": "2025-01-01",
            "manage_url": "https://example.com",
        }
        paper_no_summary = make_paper(plain_summary="")
        result = self._run_main([sub], [paper_no_summary])
        assert result == 1, "Expected exit code 1 when paper has empty summary"
