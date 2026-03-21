"""
tests/test_student_digest.py — Edge-case and guard tests for student_digest.py.

Covers failure modes identified in Round 4 QA:
  - 0-paper guard: arXiv down → no email sent, exit non-zero
  - Package ordering: category match must outrank keyword-only match
"""

from __future__ import annotations

from unittest.mock import patch

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
