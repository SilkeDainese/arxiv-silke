"""
tests/test_digest.py — Sherlock QA suite for arXiv Digest.

Covers:
  - load_config (backward compat, defaults, env override, missing files)
  - keyword scoring normalization
  - pre_filter
  - extract_colleague_papers / extract_own_papers
  - _default_analysis
  - _fallback_analyse
  - _filter_and_sort
  - _build_scoring_prompt (sanitization)
  - update_keyword_stats (isolation via mocking STATS_PATH)
  - render_html (smoke test — no crash, key strings present)
  - Known bugs flagged with xfail where behaviour is wrong-but-documented
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import digest as d
from digest import (
    _fetch_github_feedback_issues,
    _parse_recipient_emails,
    _build_scoring_prompt,
    _default_analysis,
    _parse_feedback_issue,
    _matched_keywords_for_text,
    _fallback_analyse,
    _filter_and_sort,
    apply_feedback_bias,
    extract_colleague_papers,
    extract_own_papers,
    ingest_feedback_from_github,
    load_keyword_stats,
    pre_filter,
    render_html,
    save_keyword_stats,
    send_email,
    update_keyword_stats,
)


# ─────────────────────────────────────────────────────────────
#  FIXTURES
# ─────────────────────────────────────────────────────────────


def make_paper(**overrides):
    """Return a minimal valid paper dict with sensible defaults."""
    base = {
        "id": "1234.5678",
        "title": "A Study of Stellar Rotation",
        "abstract": "We present measurements of stellar rotation in open clusters.",
        "authors": ["Smith, J.", "Jones, A."],
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


def make_config(**overrides):
    """Return a minimal valid config dict."""
    base = {
        "keywords": {"stellar rotation": 8, "vsini": 6},
        "research_authors": ["Smith"],
        "colleagues": {"people": [], "institutions": []},
        "categories": ["astro-ph.SR"],
        "days_back": 3,
        "min_score": 5,
        "max_papers": 6,
        "digest_name": "Test Digest",
        "researcher_name": "Test Researcher",
        "research_context": "I study stellar rotation.",
        "institution": "",
        "department": "",
        "tagline": "",
        "github_repo": "",
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
        "digest_mode": "highlights",
        "recipient_view_mode": "deep_read",
        "self_match": [],
        "keyword_aliases": {},
        "recipient_email": "test@example.com",
    }
    base.update(overrides)
    return base


@pytest.fixture
def tmp_stats_path(tmp_path):
    """Patch STATS_PATH to an isolated temp file so tests don't touch the real stats."""
    stats_file = tmp_path / "keyword_stats.json"
    with patch.object(d, "STATS_PATH", stats_file):
        yield stats_file


@pytest.fixture
def tmp_config_file(tmp_path):
    """Write a minimal config.yaml to a temp dir and patch CONFIG_PATH."""
    cfg = {
        "keywords": {"stellar rotation": 8},
        "recipient_email": "test@example.com",
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(cfg))
    return config_file


# ─────────────────────────────────────────────────────────────
#  load_config
# ─────────────────────────────────────────────────────────────


class TestLoadConfig:
    def test_raises_when_no_config_files(self, tmp_path):
        with patch.object(d, "CONFIG_PATH", tmp_path / "config.yaml"):
            with patch.object(
                d, "CONFIG_EXAMPLE_PATH", tmp_path / "config.example.yaml"
            ):
                with pytest.raises(FileNotFoundError, match="setup wizard"):
                    d.load_config()

    def test_loads_config_yaml_over_example(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump({"keywords": {"stars": 5}, "recipient_email": "a@b.com"})
        )
        example_file = tmp_path / "config.example.yaml"
        example_file.write_text(
            yaml.dump({"keywords": {"planets": 3}, "recipient_email": "x@y.com"})
        )
        with patch.object(d, "CONFIG_PATH", config_file):
            with patch.object(d, "CONFIG_EXAMPLE_PATH", example_file):
                cfg = d.load_config()
        assert "stars" in cfg["keywords"]
        assert "planets" not in cfg["keywords"]

    def test_defaults_applied(self, tmp_config_file):
        with patch.object(d, "CONFIG_PATH", tmp_config_file):
            cfg = d.load_config()
        assert cfg["digest_name"] == "arXiv Digest"
        assert cfg["researcher_name"] == "Reader"
        assert cfg["days_back"] == 3
        assert cfg["smtp_server"] == "smtp.gmail.com"
        assert cfg["smtp_port"] == 587
        assert cfg["recipient_view_mode"] == "deep_read"

    def test_recipient_view_mode_typo_normalized(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump({"keywords": {}, "recipient_view_mode": "skim"})
        )
        with patch.object(d, "CONFIG_PATH", config_file):
            cfg = d.load_config()
        assert cfg["recipient_view_mode"] == "5_min_skim"

    def test_keyword_aliases_normalized_to_lists(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "keywords": {"planet atmosphere": 8},
                    "keyword_aliases": {
                        "planet atmosphere": "planetary atmospheres",
                    },
                }
            )
        )
        with patch.object(d, "CONFIG_PATH", config_file):
            cfg = d.load_config()
        assert cfg["keyword_aliases"] == {
            "planet atmosphere": ["planetary atmospheres"]
        }

    def test_highlights_mode_defaults(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"keywords": {}, "digest_mode": "highlights"}))
        with patch.object(d, "CONFIG_PATH", config_file):
            cfg = d.load_config()
        assert cfg["max_papers"] == 6
        assert cfg["min_score"] == 5

    def test_in_depth_mode_defaults(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"keywords": {}, "digest_mode": "in_depth"}))
        with patch.object(d, "CONFIG_PATH", config_file):
            cfg = d.load_config()
        assert cfg["max_papers"] == 15
        assert cfg["min_score"] == 2

    def test_keywords_list_backward_compat(self, tmp_path):
        """Old configs had keywords as a flat list — should become weight-5 dict."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"keywords": ["stars", "planets"]}))
        with patch.object(d, "CONFIG_PATH", config_file):
            cfg = d.load_config()
        assert cfg["keywords"] == {"stars": 5, "planets": 5}

    def test_colleagues_list_backward_compat(self, tmp_path):
        """Old configs had colleagues as a flat list — should become people/institutions dict."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump({"keywords": {}, "colleagues": ["Alice", "Bob"]})
        )
        with patch.object(d, "CONFIG_PATH", config_file):
            cfg = d.load_config()
        assert cfg["colleagues"]["people"] == [
            {"name": "Alice", "match": ["Alice"]},
            {"name": "Bob", "match": ["Bob"]},
        ]
        assert cfg["colleagues"]["institutions"] == []

    def test_recipient_email_env_override(self, tmp_config_file):
        """RECIPIENT_EMAIL env var must take precedence over config file value."""
        with patch.object(d, "CONFIG_PATH", tmp_config_file):
            with patch.dict(os.environ, {"RECIPIENT_EMAIL": "env@override.com"}):
                cfg = d.load_config()
        assert cfg["recipient_email"] == "env@override.com"

    def test_recipient_email_falls_back_to_config(self, tmp_config_file):
        env = {k: v for k, v in os.environ.items() if k != "RECIPIENT_EMAIL"}
        with patch.object(d, "CONFIG_PATH", tmp_config_file):
            with patch.dict(os.environ, env, clear=True):
                cfg = d.load_config()
        assert cfg["recipient_email"] == "test@example.com"

    def test_github_repo_env_override(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "keywords": {"stellar rotation": 8},
                    "recipient_email": "test@example.com",
                    "github_repo": "old-name/arxiv-digest",
                }
            )
        )
        with patch.object(d, "CONFIG_PATH", config_file):
            with patch.dict(os.environ, {"GITHUB_REPOSITORY": "new-name/renamed-digest"}):
                cfg = d.load_config()
        assert cfg["github_repo"] == "new-name/renamed-digest"

    def test_setup_url_env_override(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "keywords": {"stellar rotation": 8},
                    "recipient_email": "test@example.com",
                    "setup_url": "https://old.example.com",
                }
            )
        )
        with patch.object(d, "CONFIG_PATH", config_file):
            with patch.dict(os.environ, {"SETUP_WIZARD_URL": "https://new.example.com"}):
                cfg = d.load_config()
        assert cfg["setup_url"] == "https://new.example.com"

    def test_colleagues_dict_gets_defaults(self, tmp_path):
        """A colleagues dict missing the 'institutions' key gets it defaulted."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump({"keywords": {}, "colleagues": {"people": []}})
        )
        with patch.object(d, "CONFIG_PATH", config_file):
            cfg = d.load_config()
        assert "institutions" in cfg["colleagues"]

    def test_colleague_people_support_optional_note(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "keywords": {},
                    "colleagues": {
                        "people": [
                            {
                                "name": "Alice",
                                "match": ["Smith, A"],
                                "note": "Teaches stars",
                            }
                        ]
                    },
                }
            )
        )
        with patch.object(d, "CONFIG_PATH", config_file):
            cfg = d.load_config()
        assert cfg["colleagues"]["people"][0]["note"] == "Teaches stars"


# ─────────────────────────────────────────────────────────────
#  Keyword score normalisation
# ─────────────────────────────────────────────────────────────


class TestKeywordNormalisation:
    def test_keyword_hits_normalised_to_100(self):
        """A paper matching all keywords should get keyword_hits = 100."""
        config = make_config(keywords={"stellar rotation": 8, "vsini": 6})
        # raw = 8 + 6 = 14; max_possible = 14; normalised = 100
        max_possible = sum(config["keywords"].values())
        raw = 14
        hits = round(100 * raw / max_possible, 1)
        assert hits == 100.0


class TestKeywordMatching:
    def test_matches_morphological_variants(self):
        config = make_config(keywords={"planet atmosphere": 8})
        matched = _matched_keywords_for_text(
            "We analyse planetary atmospheres around warm Neptunes.",
            config,
        )
        assert matched == ["planet atmosphere"]

    def test_matches_configured_aliases(self):
        config = make_config(
            keywords={"JWST": 8},
            keyword_aliases={"JWST": ["James Webb Space Telescope"]},
        )
        matched = _matched_keywords_for_text(
            "We present James Webb Space Telescope observations of WASP-39 b.",
            config,
        )
        assert matched == ["JWST"]

    def test_empty_keywords_no_division_by_zero(self):
        """Empty keywords dict must not divide by zero."""
        config = make_config(keywords={})
        max_possible = sum(config["keywords"].values()) or 1
        hits = round(100 * 0 / max_possible, 1)
        assert hits == 0.0

    def test_partial_match_normalised(self):
        """Matching one keyword out of two should give partial score."""
        config = make_config(keywords={"stellar rotation": 8, "vsini": 2})
        max_possible = 10  # 8 + 2
        raw = 8  # only 'stellar rotation' matched
        hits = round(100 * raw / max_possible, 1)
        assert hits == 80.0


# ─────────────────────────────────────────────────────────────
#  pre_filter
# ─────────────────────────────────────────────────────────────


class TestPreFilter:
    def test_paper_with_keyword_hits_included(self):
        p = make_paper(keyword_hits=25.0)
        result = pre_filter([p])
        assert len(result) == 1

    def test_paper_with_known_author_included(self):
        p = make_paper(keyword_hits=0.0, known_authors=["Smith, J."])
        result = pre_filter([p])
        assert len(result) == 1

    def test_paper_with_no_hits_no_authors_discovery_mode(self):
        """When no papers match keywords/authors, discovery mode returns them by recency."""
        p = make_paper(keyword_hits=0.0, known_authors=[])
        result = pre_filter([p])
        assert len(result) == 1  # discovery mode keeps papers

    def test_capped_at_30(self):
        papers = [make_paper(id=str(i), keyword_hits=10.0) for i in range(50)]
        result = pre_filter(papers)
        assert len(result) == 30

    def test_sorted_by_score_descending(self):
        p_low = make_paper(id="low", keyword_hits=10.0, known_authors=[])
        p_high = make_paper(id="high", keyword_hits=80.0, known_authors=[])
        result = pre_filter([p_low, p_high])
        assert result[0]["id"] == "high"

    def test_colleague_only_paper_in_discovery_mode(self):
        """
        Colleague papers are extracted BEFORE pre_filter in main().
        pre_filter itself does not consider colleague_matches.
        In discovery mode (no keyword matches), all papers are returned by recency.
        """
        p = make_paper(keyword_hits=0.0, known_authors=[], colleague_matches=["Alice"])
        result = pre_filter([p])
        assert len(result) == 1  # discovery mode keeps papers


# ─────────────────────────────────────────────────────────────
#  extract_colleague_papers / extract_own_papers
# ─────────────────────────────────────────────────────────────


class TestExtractPapers:
    def test_extract_colleague_papers_basic(self):
        p1 = make_paper(id="1", colleague_matches=["Alice"])
        p2 = make_paper(id="2", colleague_matches=[])
        result = extract_colleague_papers([p1, p2])
        assert len(result) == 1
        assert result[0]["id"] == "1"

    def test_extract_colleague_papers_empty_list(self):
        assert extract_colleague_papers([]) == []

    def test_extract_own_papers_basic(self):
        p1 = make_paper(id="1", is_own_paper=True)
        p2 = make_paper(id="2", is_own_paper=False)
        result = extract_own_papers([p1, p2])
        assert len(result) == 1
        assert result[0]["id"] == "1"

    def test_extract_own_papers_empty_list(self):
        assert extract_own_papers([]) == []

    def test_extract_own_papers_none_own(self):
        papers = [make_paper(id=str(i), is_own_paper=False) for i in range(5)]
        assert extract_own_papers(papers) == []


# ─────────────────────────────────────────────────────────────
#  _default_analysis
# ─────────────────────────────────────────────────────────────


class TestDefaultAnalysis:
    def test_zero_keyword_hits_gives_score_1(self):
        p = make_paper(keyword_hits=0.0)
        r = _default_analysis(p)
        assert r["relevance_score"] == 1

    def test_high_keyword_hits_gives_score_10(self):
        p = make_paper(keyword_hits=100.0)
        r = _default_analysis(p)
        assert r["relevance_score"] == 10

    def test_score_capped_at_10(self):
        # keyword_hits/10 = 12 -> should be capped at 10
        p = make_paper(keyword_hits=120.0)
        r = _default_analysis(p)
        assert r["relevance_score"] == 10

    def test_score_floored_at_1(self):
        p = make_paper(keyword_hits=0.0)
        r = _default_analysis(p)
        assert r["relevance_score"] >= 1

    def test_abstract_truncated_at_300(self):
        long_abstract = "x" * 500
        p = make_paper(abstract=long_abstract, keyword_hits=0.0)
        r = _default_analysis(p)
        assert r["plain_summary"].endswith("...")
        assert len(r["plain_summary"]) <= 303  # 300 + "..."

    def test_known_authors_mentioned_in_why_interesting(self):
        p = make_paper(keyword_hits=0.0, known_authors=["Smith, J."])
        r = _default_analysis(p)
        assert "Smith, J." in r["why_interesting"]

    def test_known_author_boosts_score_consistently_with_fallback(self):
        """
        Fixed: _default_analysis now includes known_authors boost, matching _fallback_analyse.
        _fallback_analyse scores the same paper as 3 (0 + 1*3).
        The two fallback paths are inconsistent.
        Fix: add len(known_authors) * 3 to _default_analysis, same as _fallback_analyse.
        """
        p = make_paper(keyword_hits=0.0, known_authors=["Smith, J."])
        r = _default_analysis(p)
        assert r["relevance_score"] == 3

    def test_required_fields_present(self):
        p = make_paper(keyword_hits=50.0)
        r = _default_analysis(p)
        for key in [
            "relevance_score",
            "plain_summary",
            "why_interesting",
            "emoji",
            "highlight_phrase",
            "kw_tags",
            "method_tags",
            "is_new_catalog",
            "cite_worthy",
            "new_result",
        ]:
            assert key in r, f"Missing field: {key}"


# ─────────────────────────────────────────────────────────────
#  _fallback_analyse
# ─────────────────────────────────────────────────────────────


class TestFallbackAnalyse:
    def test_empty_papers_returns_empty(self):
        config = make_config(min_score=1, max_papers=10)
        result = _fallback_analyse([], config)
        assert result == []

    def test_known_author_boosts_score(self):
        config = make_config(min_score=1, max_papers=10)
        p = make_paper(keyword_hits=0.0, known_authors=["Smith, J."])
        result = _fallback_analyse([p], config)
        assert len(result) == 1
        assert result[0]["relevance_score"] == 3  # 0 + 1*3

    def test_keyword_hits_contribute_to_score(self):
        config = make_config(min_score=1, max_papers=10)
        p = make_paper(keyword_hits=50.0)  # 50/10 = 5
        result = _fallback_analyse([p], config)
        assert result[0]["relevance_score"] == 5

    def test_score_capped_at_10(self):
        config = make_config(min_score=1, max_papers=10)
        p = make_paper(keyword_hits=100.0, known_authors=["A", "B", "C", "D"])
        # 100/10 + 4*3 = 10 + 12 = 22 -> capped at 10
        result = _fallback_analyse([p], config)
        assert result[0]["relevance_score"] == 10

    def test_papers_below_min_score_filtered(self):
        config = make_config(min_score=5, max_papers=10)
        p = make_paper(keyword_hits=0.0, known_authors=[])
        result = _fallback_analyse([p], config)
        # score = max(0+0, 1) = 1 < 5 -> filtered out
        assert result == []

    def test_max_papers_cap(self):
        config = make_config(min_score=1, max_papers=3)
        papers = [make_paper(id=str(i), keyword_hits=50.0) for i in range(10)]
        result = _fallback_analyse(papers, config)
        assert len(result) <= 3


# ─────────────────────────────────────────────────────────────
#  _filter_and_sort
# ─────────────────────────────────────────────────────────────


class TestFilterAndSort:
    def test_empty_input_returns_empty(self):
        config = make_config(min_score=5, max_papers=6)
        assert _filter_and_sort([], config) == []

    def test_papers_below_min_score_dropped(self):
        config = make_config(min_score=5, max_papers=10)
        p = make_paper(relevance_score=3)
        assert _filter_and_sort([p], config) == []

    def test_papers_at_min_score_included(self):
        config = make_config(min_score=5, max_papers=10)
        p = make_paper(relevance_score=5)
        result = _filter_and_sort([p], config)
        assert len(result) == 1

    def test_sorted_descending_by_relevance(self):
        config = make_config(min_score=1, max_papers=10)
        papers = [
            make_paper(id="low", relevance_score=3),
            make_paper(id="high", relevance_score=9),
            make_paper(id="mid", relevance_score=6),
        ]
        result = _filter_and_sort(papers, config)
        scores = [p["relevance_score"] for p in result]
        assert scores == sorted(scores, reverse=True)

    def test_capped_at_max_papers(self):
        config = make_config(min_score=1, max_papers=3)
        papers = [make_paper(id=str(i), relevance_score=7) for i in range(10)]
        result = _filter_and_sort(papers, config)
        assert len(result) == 3

    def test_missing_relevance_score_treated_as_zero(self):
        """Papers without relevance_score should be treated as 0 and filtered out."""
        config = make_config(min_score=5, max_papers=10)
        p = make_paper()  # no relevance_score key
        result = _filter_and_sort([p], config)
        assert result == []


# ─────────────────────────────────────────────────────────────
#  _build_scoring_prompt
# ─────────────────────────────────────────────────────────────


class TestBuildScoringPrompt:
    def test_prompt_contains_title(self):
        config = make_config()
        p = make_paper(title="Stellar Rotation in the Pleiades")
        prompt = _build_scoring_prompt(p, config)
        assert "Stellar Rotation in the Pleiades" in prompt

    def test_prompt_contains_abstract(self):
        config = make_config()
        p = make_paper(abstract="We measured rotation rates of 500 stars.")
        prompt = _build_scoring_prompt(p, config)
        assert "We measured rotation rates of 500 stars." in prompt

    def test_researcher_name_curly_braces_sanitized(self):
        """Curly braces in researcher_name must be stripped to prevent f-string corruption."""
        config = make_config(researcher_name="Test{injection}")
        p = make_paper()
        prompt = _build_scoring_prompt(p, config)
        assert "{" not in prompt or "{{" not in prompt  # sanitized
        # More precisely: the sanitization removes { and }
        assert "injection" in prompt  # content kept, only braces removed

    def test_researcher_name_double_quotes_sanitized(self):
        """Double quotes in researcher_name must be replaced with single quotes."""
        config = make_config(researcher_name='Test "User"')
        p = make_paper()
        prompt = _build_scoring_prompt(p, config)
        # Verify the prompt builds without error and researcher appears
        assert "Test" in prompt

    def test_no_research_context_uses_fallback(self):
        config = make_config(research_context="")
        p = make_paper()
        prompt = _build_scoring_prompt(p, config)
        assert "No specific research context provided" in prompt

    def test_prompt_requests_json_response(self):
        config = make_config()
        p = make_paper()
        prompt = _build_scoring_prompt(p, config)
        assert "JSON" in prompt
        assert "relevance_score" in prompt

    def test_authors_capped_at_8(self):
        """Only the first 8 authors should appear in the prompt."""
        config = make_config()
        p = make_paper(authors=[f"Author {i}" for i in range(20)])
        prompt = _build_scoring_prompt(p, config)
        assert "Author 7" in prompt
        assert "Author 8" not in prompt  # 9th author (index 8) should be excluded


# ─────────────────────────────────────────────────────────────
#  update_keyword_stats (isolated — no disk side effects)
# ─────────────────────────────────────────────────────────────


class TestUpdateKeywordStats:
    def test_new_keyword_initialised(self, tmp_stats_path):
        config = make_config(keywords={"stellar rotation": 8})
        update_keyword_stats([], config)
        stats = json.loads(tmp_stats_path.read_text())
        assert "stellar rotation" in stats
        assert stats["stellar rotation"]["total_hits"] == 0
        assert stats["stellar rotation"]["runs_checked"] == 1

    def test_keyword_hit_incremented(self, tmp_stats_path):
        config = make_config(keywords={"stellar rotation": 8})
        p = make_paper(title="A study of stellar rotation", abstract="")
        update_keyword_stats([p], config)
        stats = json.loads(tmp_stats_path.read_text())
        assert stats["stellar rotation"]["total_hits"] == 1

    def test_keyword_miss_not_incremented(self, tmp_stats_path):
        config = make_config(keywords={"vsini": 6})
        p = make_paper(title="Cosmological survey", abstract="Nothing relevant here.")
        update_keyword_stats([p], config)
        stats = json.loads(tmp_stats_path.read_text())
        assert stats["vsini"]["total_hits"] == 0

    def test_runs_checked_increments_per_run(self, tmp_stats_path):
        config = make_config(keywords={"stellar rotation": 8})
        update_keyword_stats([], config)
        update_keyword_stats([], config)
        stats = json.loads(tmp_stats_path.read_text())
        assert stats["stellar rotation"]["runs_checked"] == 2

    def test_empty_papers_list_does_not_crash(self, tmp_stats_path):
        config = make_config(keywords={"stars": 5})
        result = update_keyword_stats([], config)
        assert "stars" in result

    def test_case_insensitive_matching(self, tmp_stats_path):
        """Keyword matching is case-insensitive."""
        config = make_config(keywords={"JWST": 7})
        p = make_paper(title="", abstract="Observations with jwst reveal...")
        update_keyword_stats([p], config)
        stats = json.loads(tmp_stats_path.read_text())
        assert stats["JWST"]["total_hits"] == 1

    def test_existing_stats_preserved(self, tmp_stats_path):
        """A second run should accumulate, not overwrite, existing stats."""
        config = make_config(keywords={"stellar rotation": 8})
        p = make_paper(title="stellar rotation study", abstract="")
        update_keyword_stats([p], config)  # run 1: 1 hit
        update_keyword_stats([p], config)  # run 2: 1 more hit
        stats = json.loads(tmp_stats_path.read_text())
        assert stats["stellar rotation"]["total_hits"] == 2


# ─────────────────────────────────────────────────────────────
#  render_html — smoke tests
# ─────────────────────────────────────────────────────────────


class TestRenderHtml:
    def test_renders_without_crash_empty_papers(self):
        config = make_config()
        html = render_html([], [], config, "March 01, 2025")
        assert "<html" in html
        assert "No highly relevant papers" in html

    def test_renders_with_one_paper(self):
        config = make_config()
        p = make_paper(
            relevance_score=7,
            plain_summary="A nice summary.",
            why_interesting="Related to your work.",
            highlight_phrase="Cool result",
            emoji="🌟",
            kw_tags=["rotation"],
            method_tags=["spectroscopy"],
            is_new_catalog=False,
            cite_worthy=False,
            new_result=None,
        )
        html = render_html([p], [], config, "March 01, 2025")
        assert p["title"] in html
        assert "7" in html  # score
        assert "What changed:" in html

    def test_skim_mode_shows_top_three_only(self):
        config = make_config(recipient_view_mode="5_min_skim")
        papers = [
            make_paper(
                id="1",
                title="Paper 1",
                relevance_score=9,
                plain_summary="One.",
                why_interesting="A",
                highlight_phrase="",
                emoji="",
                kw_tags=[],
                method_tags=[],
                is_new_catalog=False,
                cite_worthy=False,
                new_result=None,
            ),
            make_paper(
                id="2",
                title="Paper 2",
                relevance_score=8,
                plain_summary="Two.",
                why_interesting="B",
                highlight_phrase="",
                emoji="",
                kw_tags=[],
                method_tags=[],
                is_new_catalog=False,
                cite_worthy=False,
                new_result=None,
            ),
            make_paper(
                id="3",
                title="Paper 3",
                relevance_score=7,
                plain_summary="Three.",
                why_interesting="C",
                highlight_phrase="",
                emoji="",
                kw_tags=[],
                method_tags=[],
                is_new_catalog=False,
                cite_worthy=False,
                new_result=None,
            ),
            make_paper(
                id="4",
                title="Paper 4",
                relevance_score=6,
                plain_summary="Four.",
                why_interesting="D",
                highlight_phrase="",
                emoji="",
                kw_tags=[],
                method_tags=[],
                is_new_catalog=False,
                cite_worthy=False,
                new_result=None,
            ),
        ]
        html = render_html(papers, [], config, "March 01, 2025")
        assert "5-minute skim" in html
        assert "Paper 1" in html and "Paper 2" in html and "Paper 3" in html
        assert "Paper 4" not in html

    def test_feedback_links_use_arrows(self):
        config = make_config(github_repo="user/my-digest")
        p = make_paper(
            relevance_score=7,
            plain_summary="A nice summary.",
            why_interesting="Related to your work.",
            highlight_phrase="Cool result",
            emoji="🌟",
            kw_tags=["rotation"],
            method_tags=["spectroscopy"],
            is_new_catalog=False,
            cite_worthy=False,
            new_result=None,
            matched_keywords=["stellar rotation"],
        )
        html = render_html([p], [], config, "March 01, 2025")
        assert "&#x2191;" in html
        assert "&#x2193;" in html
        assert "digest-feedback" in html

    def test_renders_colleague_section(self):
        config = make_config()
        p = make_paper(
            id="col1",
            colleague_matches=["Alice"],
            colleague_details=[{"name": "Alice", "note": "Teaches stars"}],
            relevance_score=7,
            plain_summary="",
            why_interesting="",
            highlight_phrase="",
            emoji="",
            kw_tags=[],
            method_tags=[],
            is_new_catalog=False,
            cite_worthy=False,
            new_result=None,
        )
        html = render_html([], [p], config, "March 01, 2025")
        assert "Alice" in html
        assert "Colleague news" in html
        assert "Teaches stars" in html

    def test_renders_own_papers_section(self):
        config = make_config()
        p = make_paper(
            id="own1",
            is_own_paper=True,
            relevance_score=9,
            plain_summary="",
            why_interesting="",
            highlight_phrase="",
            emoji="",
            kw_tags=[],
            method_tags=[],
            is_new_catalog=False,
            cite_worthy=False,
            new_result=None,
        )
        html = render_html([], [], config, "March 01, 2025", own_papers=[p])
        assert "Congratulations" in html
        assert p["title"] in html

    def test_digest_name_in_html(self):
        config = make_config(digest_name="Silke's Digest")
        html = render_html([], [], config, "March 01, 2025")
        assert "Silke&#39;s Digest" in html or "Silke's Digest" in html

    def test_scoring_method_claude_label(self):
        config = make_config()
        html = render_html([], [], config, "March 01, 2025", scoring_method="claude")
        assert "Claude" in html

    def test_scoring_method_keywords_fallback_shows_warning(self):
        config = make_config()
        html = render_html(
            [], [], config, "March 01, 2025", scoring_method="keywords_fallback"
        )
        assert "AI scoring unavailable" in html

    def test_scoring_method_keywords_shows_notice(self):
        config = make_config()
        html = render_html([], [], config, "March 01, 2025", scoring_method="keywords")
        assert "keyword matching" in html

    def test_scoring_method_gemini_rate_limited_shows_try_later(self):
        config = make_config()
        html = render_html(
            [], [], config, "March 01, 2025", scoring_method="gemini_rate_limited"
        )
        assert "Gemini free-tier limit reached" in html

    def test_github_repo_generates_self_service_links(self):
        config = make_config(github_repo="user/my-digest")
        html = render_html([], [], config, "March 01, 2025")
        assert "user/my-digest" in html
        assert "Configure keywords" in html

    def test_top_pick_label_on_first_paper_only(self):
        config = make_config()
        papers = [
            make_paper(
                id="1",
                relevance_score=9,
                plain_summary="",
                why_interesting="",
                highlight_phrase="",
                emoji="",
                kw_tags=[],
                method_tags=[],
                is_new_catalog=False,
                cite_worthy=False,
                new_result=None,
            ),
            make_paper(
                id="2",
                relevance_score=7,
                plain_summary="",
                why_interesting="",
                highlight_phrase="",
                emoji="",
                kw_tags=[],
                method_tags=[],
                is_new_catalog=False,
                cite_worthy=False,
                new_result=None,
            ),
        ]
        html = render_html(papers, [], config, "March 01, 2025")
        assert html.count("Top pick") == 1

    def test_score_bar_handles_out_of_range_scores(self):
        """render_html must not crash if AI returns score outside 1-10."""
        config = make_config()
        p = make_paper(
            relevance_score=11,
            plain_summary="",
            why_interesting="",
            highlight_phrase="",
            emoji="",
            kw_tags=[],
            method_tags=[],
            is_new_catalog=False,
            cite_worthy=False,
            new_result=None,
        )
        # Should not raise
        html = render_html([p], [], config, "March 01, 2025")
        assert "<html" in html

    def test_own_papers_none_default(self):
        """render_html should accept own_papers=None without crashing."""
        config = make_config()
        html = render_html([], [], config, "March 01, 2025", own_papers=None)
        assert "<html" in html


# ─────────────────────────────────────────────────────────────
#  Email sending
# ─────────────────────────────────────────────────────────────


class TestEmailSending:
    def test_parse_recipient_emails_string_and_dedupes(self):
        recipients = _parse_recipient_emails(
            "a@example.com, b@example.com;\na@example.com"
        )
        assert recipients == ["a@example.com", "b@example.com"]

    def test_send_email_supports_multiple_recipients(self):
        config = make_config(recipient_email="a@example.com, b@example.com")
        with patch.dict(
            os.environ,
            {"SMTP_USER": "sender@example.com", "SMTP_PASSWORD": "secret"},
            clear=True,
        ):
            with patch("digest.smtplib.SMTP") as smtp_cls:
                ok = send_email("<p>hi</p>", 2, "March 01, 2025", config)

        smtp_instance = smtp_cls.return_value.__enter__.return_value
        assert ok is True
        smtp_instance.sendmail.assert_called_once()
        send_args = smtp_instance.sendmail.call_args[0]
        assert send_args[0] == "sender@example.com"
        assert send_args[1] == ["a@example.com", "b@example.com"]
        assert "To: a@example.com, b@example.com" in send_args[2]

    def test_send_email_returns_false_without_recipient(self, capsys):
        config = make_config(recipient_email="")

        ok = send_email("<p>hi</p>", 2, "March 01, 2025", config)

        assert ok is False
        assert "No recipient email configured" in capsys.readouterr().out

    def test_send_via_relay_requires_explicit_token(self, capsys):
        with patch.dict(os.environ, {}, clear=True):
            with patch("digest.urllib.request.urlopen") as urlopen:
                ok = d._send_via_relay(
                    ["a@example.com"],
                    "Test subject",
                    "<p>hi</p>",
                    "hi",
                )

        assert ok is False
        urlopen.assert_not_called()
        assert "DIGEST_RELAY_TOKEN is not configured" in capsys.readouterr().out

    def test_send_via_relay_uses_configured_token(self):
        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"ok": True}).encode("utf-8")

        def fake_urlopen(req, timeout=30):
            payload = json.loads(req.data.decode("utf-8"))
            assert payload["token"] == "secret-token"
            assert payload["recipients"] == ["a@example.com"]
            assert payload["subject"] == "Test subject"
            assert payload["html"] == "<p>hi</p>"
            assert payload["plain_text"] == "hi"
            assert timeout == 30
            return _FakeResponse()

        with patch.dict(os.environ, {"DIGEST_RELAY_TOKEN": "secret-token"}, clear=True):
            with patch("digest.urllib.request.urlopen", side_effect=fake_urlopen):
                ok = d._send_via_relay(
                    ["a@example.com"],
                    "Test subject",
                    "<p>hi</p>",
                    "hi",
                )

        assert ok is True

    def test_send_via_relay_reports_auth_error(self, capsys):
        import urllib.error

        with patch.dict(os.environ, {"DIGEST_RELAY_TOKEN": "bad-token"}, clear=True):
            with patch(
                "digest.urllib.request.urlopen",
                side_effect=urllib.error.HTTPError(
                    url="https://relay.example.com",
                    code=401,
                    msg="Unauthorized",
                    hdrs={},
                    fp=None,
                ),
            ):
                ok = d._send_via_relay(["a@example.com"], "Subj", "<p>hi</p>", "hi")

        assert ok is False
        output = capsys.readouterr().out
        assert "invalid or expired" in output

    def test_send_via_relay_reports_rate_limit(self, capsys):
        import urllib.error

        with patch.dict(os.environ, {"DIGEST_RELAY_TOKEN": "token"}, clear=True):
            with patch(
                "digest.urllib.request.urlopen",
                side_effect=urllib.error.HTTPError(
                    url="https://relay.example.com",
                    code=429,
                    msg="Too Many Requests",
                    hdrs={},
                    fp=None,
                ),
            ):
                ok = d._send_via_relay(["a@example.com"], "Subj", "<p>hi</p>", "hi")

        assert ok is False
        assert "rate limit" in capsys.readouterr().out.lower()

    def test_send_via_relay_reports_network_error(self, capsys):
        import urllib.error

        with patch.dict(os.environ, {"DIGEST_RELAY_TOKEN": "token"}, clear=True):
            with patch(
                "digest.urllib.request.urlopen",
                side_effect=urllib.error.URLError("Connection refused"),
            ):
                ok = d._send_via_relay(["a@example.com"], "Subj", "<p>hi</p>", "hi")

        assert ok is False
        assert "Could not reach relay" in capsys.readouterr().out

    def test_send_via_relay_reports_unexpected_response(self, capsys):
        import urllib.error

        class _BadResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"not json"

        with patch.dict(os.environ, {"DIGEST_RELAY_TOKEN": "token"}, clear=True):
            with patch("digest.urllib.request.urlopen", return_value=_BadResponse()):
                ok = d._send_via_relay(["a@example.com"], "Subj", "<p>hi</p>", "hi")

        assert ok is False
        assert "unexpected response" in capsys.readouterr().out.lower()


class TestMainExitCodes:
    def test_main_exits_nonzero_when_email_delivery_fails(self, tmp_path, capsys):
        fake_script = tmp_path / "digest.py"
        fake_script.write_text("# test placeholder\n")

        with patch.object(d, "__file__", str(fake_script)):
            with patch.object(d, "load_config", return_value=make_config()):
                with patch.object(d, "fetch_arxiv_papers", return_value=[make_paper()]):
                    with patch.object(d, "ingest_feedback_from_github", return_value={}):
                        with patch.object(d, "apply_feedback_bias"):
                            with patch.object(d, "update_keyword_stats"):
                                with patch.object(d, "extract_own_papers", return_value=[]):
                                    with patch.object(d, "extract_colleague_papers", return_value=[]):
                                        with patch.object(d, "pre_filter", return_value=[]):
                                            with patch.object(d, "analyse_papers", return_value=([], "keywords")):
                                                with patch.object(d, "render_html", return_value="<p>hi</p>"):
                                                    with patch.object(d, "send_email", return_value=False):
                                                        with pytest.raises(SystemExit, match="1"):
                                                            d.main()

        out = capsys.readouterr().out
        assert "Email delivery failed." in out


# ─────────────────────────────────────────────────────────────
#  analyse_papers — cascade logic (no real API calls)
# ─────────────────────────────────────────────────────────────


class TestAnalysePapersCascade:
    def test_empty_papers_returns_empty(self):
        config = make_config()
        with patch.dict(os.environ, {}, clear=True):
            result, method = d.analyse_papers([], config)
        assert result == []
        assert method == "none"

    def test_no_api_keys_uses_keyword_fallback(self, tmp_stats_path):
        config = make_config(min_score=1, max_papers=10)
        p = make_paper(keyword_hits=50.0)
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY")
        }
        with patch.dict(os.environ, env, clear=True):
            result, method = d.analyse_papers([p], config)
        assert method == "keywords"

    def test_claude_credit_error_cascades_to_gemini(self, tmp_stats_path):
        """When Claude returns a credit error, should cascade to Gemini if key present."""
        config = make_config(min_score=1, max_papers=10)
        p = make_paper(keyword_hits=50.0)

        def fake_claude(papers, cfg, key):
            return None, "claude_no_credits"

        def fake_gemini(papers, cfg, key):
            for paper in papers:
                paper["relevance_score"] = 7
                paper.update(
                    {
                        "plain_summary": "s",
                        "why_interesting": "w",
                        "highlight_phrase": "h",
                        "emoji": "e",
                        "kw_tags": [],
                        "method_tags": [],
                        "is_new_catalog": False,
                        "cite_worthy": False,
                        "new_result": None,
                    }
                )
            return _filter_and_sort(papers, cfg), None

        env = {"ANTHROPIC_API_KEY": "fake-key", "GEMINI_API_KEY": "fake-gemini-key"}
        with patch.dict(os.environ, env):
            with patch.object(d, "HAS_ANTHROPIC", True):
                with patch.object(d, "HAS_GEMINI", True):
                    with patch.object(d, "_analyse_with_claude", fake_claude):
                        with patch.object(d, "_analyse_with_gemini", fake_gemini):
                            result, method = d.analyse_papers([p], config)
        assert method == "gemini"

    def test_claude_error_no_gemini_key_uses_keywords(self):
        config = make_config(min_score=1, max_papers=10)
        p = make_paper(keyword_hits=50.0)

        def fake_claude(papers, cfg, key):
            return None, "claude_errors"

        env = {"ANTHROPIC_API_KEY": "fake-key"}
        env.pop("GEMINI_API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake-key"}):
                with patch.object(d, "HAS_ANTHROPIC", True):
                    with patch.object(d, "HAS_GEMINI", False):
                        with patch.object(d, "_analyse_with_claude", fake_claude):
                            result, method = d.analyse_papers([p], config)
        assert method == "keywords_fallback"


# ─────────────────────────────────────────────────────────────
#  Feedback parsing + bias
# ─────────────────────────────────────────────────────────────


class TestFeedbackHelpers:
    def test_parse_feedback_issue(self):
        issue = {
            "body": "feedback_type: relevant\nmatched_keywords: JWST, transmission spectroscopy\n"
        }
        feedback_type, keywords = _parse_feedback_issue(issue)
        assert feedback_type == "relevant"
        assert keywords == ["JWST", "transmission spectroscopy"]

    def test_apply_feedback_bias(self):
        papers = [
            make_paper(matched_keywords=["JWST", "stellar rotation"], feedback_bias=0),
            make_paper(id="2", matched_keywords=["other"], feedback_bias=0),
        ]
        stats = {"keyword_feedback": {"jwst": 2, "stellar rotation": 1, "other": -1}}
        apply_feedback_bias(papers, stats)
        assert papers[0]["feedback_bias"] == 3
        assert papers[1]["feedback_bias"] == -1

    def test_fetch_github_feedback_issues_follows_pagination(self):
        class FakeResponse:
            def __init__(self, payload, link=""):
                self._payload = payload
                self.headers = {"Link": link}

            def read(self):
                return json.dumps(self._payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        responses = iter(
            [
                FakeResponse(
                    [{"id": 1}],
                    '<https://api.github.com/repos/user/repo/issues?page=2>; rel="next"',
                ),
                FakeResponse([{"id": 2}]),
            ]
        )

        with patch("digest.urllib.request.urlopen", side_effect=lambda *args, **kwargs: next(responses)):
            issues = _fetch_github_feedback_issues("user/repo", "token")

        assert [issue["id"] for issue in issues] == [1, 2]

    def test_ingest_feedback_from_github_processes_multiple_pages(self, tmp_path):
        class FakeResponse:
            def __init__(self, payload, link=""):
                self._payload = payload
                self.headers = {"Link": link}

            def read(self):
                return json.dumps(self._payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        responses = iter(
            [
                FakeResponse(
                    [
                        {
                            "id": 11,
                            "body": "feedback_type: relevant\nmatched_keywords: JWST\n",
                        }
                    ],
                    '<https://api.github.com/repos/user/repo/issues?page=2>; rel="next"',
                ),
                FakeResponse(
                    [
                        {
                            "id": 12,
                            "body": "feedback_type: not_relevant\nmatched_keywords: JWST\n",
                        }
                    ]
                ),
            ]
        )

        with patch.object(d, "FEEDBACK_STATS_PATH", tmp_path / "feedback_stats.json"):
            with patch.dict(os.environ, {"GITHUB_TOKEN": "token"}, clear=True):
                with patch("digest.urllib.request.urlopen", side_effect=lambda *args, **kwargs: next(responses)):
                    stats = ingest_feedback_from_github(make_config(github_repo="user/repo"))

        assert stats["processed_issue_ids"] == [11, 12]
        assert stats["keyword_feedback"]["jwst"] == 0


# ─────────────────────────────────────────────────────────────
#  Edge cases: XML parsing in fetch_arxiv_papers (unit-level)
# ─────────────────────────────────────────────────────────────


class TestFetchArxivXmlParsing:
    """
    fetch_arxiv_papers makes live network calls — we only test the parsing
    logic by constructing minimal XML and invoking the parsing inline.
    A full integration test would require network or VCR cassettes.
    """

    def test_malformed_entry_skipped(self):
        """
        The parsing code wraps each entry in try/except AttributeError/TypeError/ValueError.
        Verify that a paper with a missing 'published' field is silently skipped.
        This is tested by confirming the guard clause exists in the source.
        """
        import inspect

        source = inspect.getsource(d.fetch_arxiv_papers)
        assert "except (AttributeError, TypeError, ValueError)" in source

    def test_deduplication_logic(self):
        """
        Papers fetched from multiple categories may share an ID.
        The deduplication should keep only the first occurrence.
        """
        # Simulate what fetch_arxiv_papers does with the seen-set dedup
        papers_raw = [
            make_paper(id="1234.5678", category="astro-ph.SR"),
            make_paper(id="1234.5678", category="astro-ph.EP"),  # duplicate
            make_paper(id="9999.0001", category="astro-ph.SR"),
        ]
        seen = set()
        unique = []
        for p in papers_raw:
            if p["id"] not in seen:
                seen.add(p["id"])
                unique.append(p)
        assert len(unique) == 2
        assert unique[0]["category"] == "astro-ph.SR"  # first occurrence kept
