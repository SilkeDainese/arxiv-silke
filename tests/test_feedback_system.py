"""Tests for the central feedback store and aggregate ranking pipeline."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest import mock

import pytest


# ─────── Helpers ──────────────────────────────────────────────

def make_paper(**overrides: Any) -> dict[str, Any]:
    defaults = {
        "id": "2403.12345",
        "title": "Test Paper",
        "authors": ["Author A"],
        "category": "astro-ph.SR",
        "url": "https://arxiv.org/abs/2403.12345",
        "abstract": "A test abstract.",
        "published": datetime.now(timezone.utc).isoformat(),
        "matched_keywords": ["stellar rotation"],
        "keyword_hits": 10,
        "relevance_score": 7,
        "feedback_bias": 0,
        "student_package_ids": ["stars"],
        "student_au_priority": 0,
        "expert_net": 0,
    }
    defaults.update(overrides)
    return defaults


def make_config(**overrides: Any) -> dict[str, Any]:
    defaults = {
        "categories": ["astro-ph.SR"],
        "keywords": {"stellar rotation": 8},
        "allow_feedback_for_students": False,
        "github_repo": "",
    }
    defaults.update(overrides)
    return defaults


# ─────── relay/api/feedback.py unit tests ─────────────────────

class TestFeedbackRelay:
    """Tests for the feedback relay API dispatch logic."""

    def test_submit_requires_token(self):
        from relay.api.feedback import _handle_submit
        with mock.patch("relay.api.feedback.FEEDBACK_RELAY_TOKEN", "secret"):
            with pytest.raises(PermissionError):
                _handle_submit({"token": "wrong", "votes": [{"paper_id": "x", "vote": "up"}]})

    def test_submit_rejects_empty_votes(self):
        from relay.api.feedback import _handle_submit
        with mock.patch("relay.api.feedback.FEEDBACK_RELAY_TOKEN", "secret"):
            with pytest.raises(ValueError, match="No votes"):
                _handle_submit({"token": "secret", "votes": []})

    def test_submit_accepts_valid_votes(self):
        from relay.api.feedback import _dispatch
        mock_store = {"votes": [], "aggregated": {}}
        with (
            mock.patch("relay.api.feedback.FEEDBACK_RELAY_TOKEN", "secret"),
            mock.patch("relay.api.feedback._load_feedback_store", return_value=(mock_store, "sha123")),
            mock.patch("relay.api.feedback._save_feedback_store") as mock_save,
        ):
            status, result = _dispatch({
                "action": "submit",
                "token": "secret",
                "votes": [
                    {"paper_id": "2403.111", "vote": "up", "keywords": ["rotation"]},
                    {"paper_id": "2403.222", "vote": "down", "keywords": ["dark matter"]},
                ],
            })
            assert status == 200
            assert result["ok"] is True
            assert result["accepted"] == 2
            mock_save.assert_called_once()
            saved_store = mock_save.call_args[0][0]
            assert len(saved_store["votes"]) == 2
            assert saved_store["aggregated"]["2403.111"]["net"] == 1
            assert saved_store["aggregated"]["2403.222"]["net"] == -1

    def test_submit_caps_batch_size(self):
        from relay.api.feedback import _handle_submit
        with mock.patch("relay.api.feedback.FEEDBACK_RELAY_TOKEN", "secret"):
            with pytest.raises(ValueError, match="Too many"):
                _handle_submit({
                    "token": "secret",
                    "votes": [{"paper_id": f"id{i}", "vote": "up"} for i in range(201)],
                })

    def test_aggregate_requires_admin_token(self):
        from relay.api.feedback import _handle_aggregate
        with mock.patch("relay.api.feedback.STUDENT_ADMIN_TOKEN", "admin123"):
            with pytest.raises(PermissionError):
                _handle_aggregate({"admin_token": "wrong"})

    def test_aggregate_returns_data(self):
        from relay.api.feedback import _dispatch
        mock_store = {
            "votes": [{"paper_id": "x", "vote": "up", "keywords": [], "package_tags": [], "timestamp": "t"}],
            "aggregated": {"x": {"up": 1, "down": 0, "net": 1}},
        }
        with (
            mock.patch("relay.api.feedback.STUDENT_ADMIN_TOKEN", "admin123"),
            mock.patch("relay.api.feedback._load_feedback_store", return_value=(mock_store, None)),
        ):
            status, result = _dispatch({"action": "aggregate", "admin_token": "admin123"})
            assert status == 200
            assert result["ok"] is True
            assert result["aggregated"]["x"]["net"] == 1
            assert result["total_votes"] == 1

    def test_stats_returns_summary(self):
        from relay.api.feedback import _dispatch
        mock_store = {
            "votes": [
                {"paper_id": "a", "vote": "up", "keywords": [], "package_tags": [], "timestamp": "t"},
                {"paper_id": "b", "vote": "down", "keywords": [], "package_tags": [], "timestamp": "t"},
            ],
            "aggregated": {"a": {"net": 1}, "b": {"net": -1}},
        }
        with (
            mock.patch("relay.api.feedback.STUDENT_ADMIN_TOKEN", "admin123"),
            mock.patch("relay.api.feedback._load_feedback_store", return_value=(mock_store, None)),
        ):
            status, result = _dispatch({"action": "stats", "admin_token": "admin123"})
            assert status == 200
            assert result["total_votes"] == 2
            assert result["unique_papers"] == 2
            assert result["papers_with_positive_net"] == 1
            assert result["papers_with_negative_net"] == 1

    def test_unknown_action(self):
        from relay.api.feedback import _dispatch
        status, result = _dispatch({"action": "bogus"})
        assert status == 400


class TestReaggregate:
    """Tests for the reaggregation logic."""

    def test_reaggregate_builds_correct_tallies(self):
        from relay.api.feedback import _reaggregate
        store = {
            "votes": [
                {"paper_id": "p1", "vote": "up", "keywords": ["kw1"], "package_tags": ["stars"], "timestamp": "t1"},
                {"paper_id": "p1", "vote": "up", "keywords": ["kw1"], "package_tags": ["stars"], "timestamp": "t2"},
                {"paper_id": "p1", "vote": "down", "keywords": ["kw2"], "package_tags": [], "timestamp": "t3"},
                {"paper_id": "p2", "vote": "down", "keywords": [], "package_tags": [], "timestamp": "t1"},
            ],
            "aggregated": {},
        }
        _reaggregate(store)
        assert store["aggregated"]["p1"]["up"] == 2
        assert store["aggregated"]["p1"]["down"] == 1
        assert store["aggregated"]["p1"]["net"] == 1
        assert store["aggregated"]["p1"]["keywords"]["kw1"] == 2
        assert store["aggregated"]["p1"]["keywords"]["kw2"] == -1
        assert store["aggregated"]["p2"]["net"] == -1


# ─────── digest.py mirror + ranking integration ──────────────

class TestMirrorFeedback:
    """Tests for the mirror_feedback_to_central function."""

    def test_mirror_skips_when_opt_out(self):
        from digest import mirror_feedback_to_central
        config = make_config(allow_feedback_for_students=False)
        assert mirror_feedback_to_central({"keyword_feedback": {"kw": 1}}, config) == 0

    def test_mirror_skips_without_relay_token(self):
        from digest import mirror_feedback_to_central
        config = make_config(allow_feedback_for_students=True)
        with mock.patch.dict("os.environ", {"FEEDBACK_RELAY_TOKEN": ""}, clear=False):
            assert mirror_feedback_to_central({"keyword_feedback": {"kw": 1}}, config) == 0

    def test_mirror_skips_empty_feedback(self):
        from digest import mirror_feedback_to_central
        config = make_config(allow_feedback_for_students=True)
        with mock.patch.dict("os.environ", {"FEEDBACK_RELAY_TOKEN": "tok"}, clear=False):
            assert mirror_feedback_to_central({"keyword_feedback": {}}, config) == 0

    def test_mirror_sends_anonymised_votes(self):
        from digest import mirror_feedback_to_central
        config = make_config(
            allow_feedback_for_students=True,
            categories=["astro-ph.SR", "astro-ph.EP"],
        )
        feedback = {"keyword_feedback": {"rotation": 3, "transit": -2, "neutral_kw": 0}}

        def fake_urlopen(request, timeout=None):
            body = json.loads(request.data)
            assert body["action"] == "submit"
            assert body["token"] == "tok"
            votes = body["votes"]
            # neutral_kw should be excluded (bias == 0)
            assert len(votes) == 2
            ids = {v["paper_id"] for v in votes}
            assert "keyword_signal:rotation" in ids
            assert "keyword_signal:transit" in ids
            # Check package tags are derived from categories
            for v in votes:
                assert "stars" in v["package_tags"] or "exoplanets" in v["package_tags"]

            resp = mock.MagicMock()
            resp.read.return_value = json.dumps({"ok": True, "accepted": 2}).encode()
            resp.__enter__ = mock.MagicMock(return_value=resp)
            resp.__exit__ = mock.MagicMock(return_value=False)
            return resp

        with (
            mock.patch.dict("os.environ", {"FEEDBACK_RELAY_TOKEN": "tok"}, clear=False),
            mock.patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            assert mirror_feedback_to_central(feedback, config) == 2

    def test_mirror_handles_network_error(self):
        from digest import mirror_feedback_to_central
        config = make_config(allow_feedback_for_students=True)
        feedback = {"keyword_feedback": {"kw": 1}}
        with (
            mock.patch.dict("os.environ", {"FEEDBACK_RELAY_TOKEN": "tok"}, clear=False),
            mock.patch("urllib.request.urlopen", side_effect=ConnectionError("fail")),
        ):
            assert mirror_feedback_to_central(feedback, config) == 0


class TestCategoriesToPackageTags:
    def test_maps_known_categories(self):
        from digest import _categories_to_package_tags
        assert _categories_to_package_tags(["astro-ph.SR", "astro-ph.EP"]) == ["stars", "exoplanets"]

    def test_ignores_unknown_categories(self):
        from digest import _categories_to_package_tags
        assert _categories_to_package_tags(["hep-th", "astro-ph.CO"]) == ["cosmology"]

    def test_deduplicates(self):
        from digest import _categories_to_package_tags
        assert _categories_to_package_tags(["astro-ph.SR", "astro-ph.SR"]) == ["stars"]


# ─────── student_digest.py aggregate ranking ─────────────────

class TestAggregateExpertSignal:
    """Tests for apply_aggregate_expert_signal."""

    def test_applies_direct_paper_signal(self):
        from student_digest import apply_aggregate_expert_signal
        papers = [make_paper(id="p1"), make_paper(id="p2")]
        aggregated = {"p1": {"net": 3}, "p2": {"net": -1}}
        apply_aggregate_expert_signal(papers, aggregated)
        assert papers[0]["expert_net"] == 3
        assert papers[1]["expert_net"] == -1

    def test_applies_keyword_signal(self):
        from student_digest import apply_aggregate_expert_signal
        papers = [make_paper(matched_keywords=["rotation", "gyro"])]
        aggregated = {
            "keyword_signal:rotation": {"net": 2},
            "keyword_signal:gyro": {"net": -1},
        }
        apply_aggregate_expert_signal(papers, aggregated)
        assert papers[0]["expert_net"] == 1  # 2 + (-1)

    def test_combines_direct_and_keyword(self):
        from student_digest import apply_aggregate_expert_signal
        papers = [make_paper(id="p1", matched_keywords=["rotation"])]
        aggregated = {
            "p1": {"net": 2},
            "keyword_signal:rotation": {"net": 3},
        }
        apply_aggregate_expert_signal(papers, aggregated)
        assert papers[0]["expert_net"] == 5

    def test_handles_empty_aggregated(self):
        from student_digest import apply_aggregate_expert_signal
        papers = [make_paper()]
        apply_aggregate_expert_signal(papers, {})
        assert papers[0].get("expert_net", 0) == 0


class TestFreshnessScore:
    def test_today_is_one(self):
        from student_digest import _freshness_score
        paper = make_paper(published=datetime.now(timezone.utc).isoformat())
        assert _freshness_score(paper) == pytest.approx(1.0, abs=0.15)

    def test_week_old_is_zero(self):
        from student_digest import _freshness_score
        from datetime import timedelta
        old = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        paper = make_paper(published=old)
        assert _freshness_score(paper) == pytest.approx(0.0, abs=0.01)

    def test_missing_date(self):
        from student_digest import _freshness_score
        assert _freshness_score(make_paper(published="")) == 0.0

    def test_bare_date_string_is_nonzero(self):
        """Bare date strings like "2026-03-25" must not silently return 0.0."""
        from student_digest import _freshness_score
        from datetime import date
        today = date.today().isoformat()
        score = _freshness_score(make_paper(published=today))
        assert score > 0.0


class TestStudentRankingIntegration:
    """Test the full 4-signal ranking pipeline."""

    def test_expert_signal_boosts_ranking(self):
        from student_digest import select_student_papers
        # Same relevance_score and AU priority, but different expert_net
        p1 = make_paper(id="low", expert_net=-2, relevance_score=7)
        p2 = make_paper(id="high", expert_net=5, relevance_score=7)
        result = select_student_papers([p1, p2], ["stars"], 10)
        assert result[0]["id"] == "high"

    def test_au_priority_beats_expert_signal(self):
        from student_digest import select_student_papers
        # AU priority should outrank expert signal
        p1 = make_paper(id="au", student_au_priority=1, expert_net=0, relevance_score=5)
        p2 = make_paper(id="expert", student_au_priority=0, expert_net=10, relevance_score=9)
        result = select_student_papers([p1, p2], ["stars"], 10)
        assert result[0]["id"] == "au"

    def test_max_papers_cuts_list(self):
        from student_digest import select_student_papers
        papers = [make_paper(id=f"p{i}", relevance_score=10 - i) for i in range(10)]
        result = select_student_papers(papers, ["stars"], 3)
        assert len(result) == 3

    def test_package_filter_excludes_unmatched_non_astro(self):
        """Non-astro-ph papers are excluded unless they match a selected package."""
        from student_digest import select_student_papers
        p_astro = make_paper(student_package_ids=["stars"])
        p_ml = make_paper(category="stat.ML", student_package_ids=["methods_ml"])
        result = select_student_papers([p_astro, p_ml], ["stars"], 10)
        assert len(result) == 1

    def test_all_astro_papers_always_included(self):
        """All astro-ph.* papers are candidates regardless of package match."""
        from student_digest import select_student_papers
        p_stars = make_paper(student_package_ids=["stars"])
        p_cosmo = make_paper(student_package_ids=["cosmology"])
        result = select_student_papers([p_stars, p_cosmo], ["stars"], 10)
        assert len(result) == 2


class TestFetchAggregateFeedback:
    """Tests for fetch_aggregate_feedback."""

    def test_returns_empty_without_admin_token(self):
        from student_digest import fetch_aggregate_feedback
        with mock.patch.dict("os.environ", {"STUDENT_ADMIN_TOKEN": ""}, clear=False):
            assert fetch_aggregate_feedback() == {}

    def test_returns_data_on_success(self):
        from student_digest import fetch_aggregate_feedback

        def fake_post(*args, **kwargs):
            resp = mock.MagicMock()
            resp.json.return_value = {
                "ok": True,
                "aggregated": {"p1": {"net": 2}},
                "total_votes": 5,
            }
            resp.raise_for_status = mock.MagicMock()
            return resp

        with (
            mock.patch.dict("os.environ", {"STUDENT_ADMIN_TOKEN": "tok"}, clear=False),
            mock.patch("requests.post", side_effect=fake_post),
        ):
            result = fetch_aggregate_feedback()
            assert result == {"p1": {"net": 2}}

    def test_returns_empty_on_network_error(self):
        from student_digest import fetch_aggregate_feedback
        with (
            mock.patch.dict("os.environ", {"STUDENT_ADMIN_TOKEN": "tok"}, clear=False),
            mock.patch("urllib.request.urlopen", side_effect=ConnectionError("fail")),
        ):
            assert fetch_aggregate_feedback() == {}
