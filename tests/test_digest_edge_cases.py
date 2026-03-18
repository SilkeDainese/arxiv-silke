"""
tests/test_digest_edge_cases.py — Edge-case tests that exercise the crash
and silent-corruption paths identified in the Sherlock QA audit.

Complements test_digest.py (which covers the happy paths).
All tests use the existing make_paper / make_config helpers via direct import.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import digest as d
from digest import _default_analysis, _fallback_analyse, load_config, load_keyword_stats, load_feedback_stats, main


# ─────────────────────────────────────────────────────────────
#  Helpers (mirrors test_digest.py fixtures as plain functions
#  so this file can run standalone without conftest)
# ─────────────────────────────────────────────────────────────


def make_paper(**overrides):
    base = {
        "id": "1234.5678",
        "title": "A Study",
        "abstract": "Abstract text.",
        "authors": ["Smith, J."],
        "published": "2025-03-01",
        "category": "astro-ph.SR",
        "url": "https://arxiv.org/abs/1234.5678",
        "known_authors": [],
        "colleague_matches": [],
        "is_own_paper": False,
        "keyword_hits_raw": 0,
        "keyword_hits": 0.0,
    }
    base.update(overrides)
    return base


def _write_config(tmp_path: Path, content: str) -> Path:
    """Write a raw string to config.yaml in tmp_path."""
    f = tmp_path / "config.yaml"
    f.write_text(content)
    return f


# ─────────────────────────────────────────────────────────────
#  load_config — crash / corruption edge cases
# ─────────────────────────────────────────────────────────────


class TestLoadConfigEdgeCases:
    def test_empty_yaml_raises_value_error(self, tmp_path):
        """Empty config file must raise ValueError, not AttributeError."""
        cfg_file = _write_config(tmp_path, "")
        with patch.object(d, "CONFIG_PATH", cfg_file):
            with pytest.raises(ValueError, match="empty or not a YAML mapping"):
                load_config()

    def test_yaml_only_null_raises_value_error(self, tmp_path):
        """A file containing only `null` parses to None → must raise ValueError."""
        cfg_file = _write_config(tmp_path, "null\n")
        with patch.object(d, "CONFIG_PATH", cfg_file):
            with pytest.raises(ValueError, match="empty or not a YAML mapping"):
                load_config()

    def test_yaml_list_at_root_raises_value_error(self, tmp_path):
        """A config file that is a YAML list instead of mapping must raise ValueError."""
        cfg_file = _write_config(tmp_path, "- item1\n- item2\n")
        with patch.object(d, "CONFIG_PATH", cfg_file):
            with pytest.raises(ValueError, match="empty or not a YAML mapping"):
                load_config()

    def test_keywords_as_bare_string_raises_value_error(self, tmp_path):
        """keywords: exoplanet  (bare string) must raise ValueError, not silently
        iterate characters and produce {'e':5, 'x':5, ...}."""
        content = "keywords: exoplanet\nrecipient_email: x@example.com\n"
        cfg_file = _write_config(tmp_path, content)
        with patch.object(d, "CONFIG_PATH", cfg_file):
            with pytest.raises(ValueError, match="keywords must be a YAML mapping"):
                load_config()

    def test_colleagues_null_does_not_crash(self, tmp_path):
        """colleagues: null must not crash — it should default to empty."""
        content = (
            "keywords:\n  exoplanet: 7\n"
            "colleagues: null\n"
            "recipient_email: x@example.com\n"
        )
        cfg_file = _write_config(tmp_path, content)
        with patch.object(d, "CONFIG_PATH", cfg_file):
            cfg = load_config()
        assert isinstance(cfg["colleagues"], dict)
        assert cfg["colleagues"]["people"] == []
        assert cfg["colleagues"]["institutions"] == []

    def test_minimal_valid_config_applies_all_defaults(self, tmp_path):
        """A config with only required fields must silently acquire all defaults."""
        content = "keywords:\n  stellar rotation: 8\nrecipient_email: test@example.com\n"
        cfg_file = _write_config(tmp_path, content)
        with patch.object(d, "CONFIG_PATH", cfg_file):
            cfg = load_config()
        assert cfg["digest_name"] == "arXiv Digest"
        assert cfg["digest_mode"] == "highlights"
        assert cfg["max_papers"] == 6
        assert cfg["min_score"] == 5
        assert isinstance(cfg["colleagues"]["people"], list)


# ─────────────────────────────────────────────────────────────
#  _default_analysis — missing keyword_hits
# ─────────────────────────────────────────────────────────────


class TestDefaultAnalysisMissingKeywordHits:
    def test_paper_without_keyword_hits_does_not_raise(self):
        """If keyword_hits is absent, _default_analysis must not raise KeyError."""
        paper = make_paper()
        del paper["keyword_hits"]  # simulate missing field
        result = _default_analysis(paper)
        assert "relevance_score" in result
        assert 1 <= result["relevance_score"] <= 10

    def test_paper_with_keyword_hits_zero_scores_minimum(self):
        """A paper with zero keyword hits and no known authors gets score 1."""
        paper = make_paper(keyword_hits=0.0, known_authors=[])
        result = _default_analysis(paper)
        assert result["relevance_score"] == 1

    def test_paper_with_high_keyword_hits_scores_higher(self):
        """A paper with keyword_hits=80 should score near top."""
        paper = make_paper(keyword_hits=80.0, known_authors=[])
        result = _default_analysis(paper)
        assert result["relevance_score"] >= 7


# ─────────────────────────────────────────────────────────────
#  _fallback_analyse — missing keyword_hits
# ─────────────────────────────────────────────────────────────


class TestFallbackAnalyseMissingKeywordHits:
    def _minimal_cfg(self):
        return {
            "keywords": {"exoplanet": 8},
            "colleagues": {"people": [], "institutions": []},
            "keyword_aliases": {},
            "research_authors": [],
            "self_match": [],
            "digest_mode": "highlights",
            "min_score": 1,
            "max_papers": 10,
        }

    def test_paper_without_keyword_hits_does_not_raise(self):
        """_fallback_analyse must not crash when keyword_hits is absent."""
        paper = make_paper()
        del paper["keyword_hits"]
        cfg = self._minimal_cfg()
        papers = [paper]
        _fallback_analyse(papers, cfg)
        assert "relevance_score" in papers[0]

    def test_paper_with_keyword_hits_missing_scores_one(self):
        """Missing keyword_hits treated as 0 → minimum score."""
        paper = make_paper(known_authors=[])
        del paper["keyword_hits"]
        cfg = self._minimal_cfg()
        papers = [paper]
        _fallback_analyse(papers, cfg)
        assert papers[0]["relevance_score"] >= 1


# ─────────────────────────────────────────────────────────────
#  load_keyword_stats / load_feedback_stats — corrupted file recovery
# ─────────────────────────────────────────────────────────────


class TestStatsCorruptionRecovery:
    def test_load_keyword_stats_corrupted_returns_empty_dict(self, tmp_path):
        """Corrupted keyword_stats.json must not crash — returns {}."""
        stats_file = tmp_path / "keyword_stats.json"
        stats_file.write_text("{not valid json{{")
        with patch.object(d, "STATS_PATH", stats_file):
            result = load_keyword_stats()
        assert result == {}

    def test_load_feedback_stats_corrupted_returns_default(self, tmp_path):
        """Corrupted feedback_stats.json must not crash — returns the default structure."""
        stats_file = tmp_path / "feedback_stats.json"
        stats_file.write_text("[truncated")
        with patch.object(d, "FEEDBACK_STATS_PATH", stats_file):
            result = load_feedback_stats()
        assert result == {
            "processed_issue_ids": [],
            "keyword_feedback": {},
            "updated_at": None,
        }


# ─────────────────────────────────────────────────────────────
#  main() — 0-paper early exit (all arXiv fetches failed)
# ─────────────────────────────────────────────────────────────


class TestZeroPaperDigest:
    """When every arXiv category fetch fails, main() must exit without sending email."""

    def _minimal_config(self) -> dict:
        return {
            "keywords": {"exoplanet": 8},
            "research_authors": [],
            "colleagues": {"people": [], "institutions": []},
            "keyword_aliases": {},
            "self_match": [],
            "digest_name": "arXiv Digest",
            "digest_mode": "highlights",
            "max_papers": 6,
            "min_score": 5,
            "days_back": 7,
            "arxiv_categories": ["astro-ph.SR"],
            "recipient_email": "test@example.com",
            "recipient_view_mode": "researcher",
            "smtp_user": "",
            "smtp_password": "",
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "github_repository": "",
            "github_token": "",
            "relay_url": "",
            "setup_wizard_url": "",
            "feedback_label": "digest-feedback",
        }

    def test_zero_papers_exits_without_sending_email(self, tmp_path):
        """main() must raise SystemExit and never call send_email when 0 papers fetched."""
        with (
            patch.object(d, "load_config", return_value=self._minimal_config()),
            patch.object(d, "fetch_arxiv_papers", return_value=[]),
            patch.object(d, "send_email") as mock_send,
        ):
            with pytest.raises(SystemExit):
                main()
        mock_send.assert_not_called()
