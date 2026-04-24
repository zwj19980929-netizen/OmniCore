"""
Unit tests for utils/gap_tracker.py — cross-round perception-gap tracking.
"""
from __future__ import annotations

import pytest

from utils.gap_tracker import GapTracker, make_gap_key


# ----------------------------------------------------------------------
# make_gap_key — label normalization + role canonicalization
# ----------------------------------------------------------------------


class TestMakeGapKey:
    def test_identical_labels_produce_same_key(self):
        a = make_gap_key({"role": "button", "label": "Sign in"})
        b = make_gap_key({"role": "button", "label": "Sign in"})
        assert a == b

    def test_word_order_does_not_matter(self):
        # Sorted-token form collapses word-order variants.
        a = make_gap_key({"role": "button", "label": "agree to terms"})
        b = make_gap_key({"role": "button", "label": "terms to agree"})
        assert a == b

    def test_role_canonicalized(self):
        # HTML "submit" maps to canonical "button" role.
        a = make_gap_key({"role": "button", "label": "Go"})
        b = make_gap_key({"role": "submit", "label": "Go"})
        assert a == b

    def test_short_tokens_dropped(self):
        # Single-character tokens get stripped during tokenization so they
        # can't collide across unrelated gaps.
        a = make_gap_key({"role": "button", "label": "OK"})  # 'ok' survives (len 2)
        b = make_gap_key({"role": "button", "label": "a OK b"})
        assert a == b

    def test_malformed_returns_empty_key(self):
        assert make_gap_key("not a dict") == ("", "")
        assert make_gap_key(None) == ("", "")
        assert make_gap_key({}) == ("", "")
        assert make_gap_key({"role": "", "label": ""}) == ("", "")

    def test_different_labels_distinct(self):
        a = make_gap_key({"role": "button", "label": "Sign in"})
        b = make_gap_key({"role": "button", "label": "Sign up"})
        assert a != b

    def test_different_roles_distinct(self):
        a = make_gap_key({"role": "button", "label": "agree"})
        b = make_gap_key({"role": "checkbox", "label": "agree"})
        assert a != b


# ----------------------------------------------------------------------
# GapTracker.update — consecutive counting + page-change reset
# ----------------------------------------------------------------------


class TestGapTrackerUpdate:
    def test_initial_round_starts_at_one(self):
        t = GapTracker()
        t.update([{"role": "button", "label": "OK"}], "https://x.com/page")
        assert t.streak_for({"role": "button", "label": "OK"}) == 1

    def test_persistence_bumps_counter(self):
        t = GapTracker()
        gap = {"role": "button", "label": "OK"}
        t.update([gap], "https://x.com/page")
        t.update([gap], "https://x.com/page")
        t.update([gap], "https://x.com/page")
        assert t.streak_for(gap) == 3

    def test_absent_gap_drops_to_zero(self):
        t = GapTracker()
        gap_a = {"role": "button", "label": "Alpha"}
        gap_b = {"role": "button", "label": "Bravo"}
        # Round 1: both present.
        t.update([gap_a, gap_b], "https://x.com/p")
        assert t.streak_for(gap_a) == 1
        assert t.streak_for(gap_b) == 1
        # Round 2: only A — B should drop to 0.
        t.update([gap_a], "https://x.com/p")
        assert t.streak_for(gap_a) == 2
        assert t.streak_for(gap_b) == 0

    def test_returning_gap_starts_fresh(self):
        t = GapTracker()
        gap = {"role": "button", "label": "Flicker"}
        t.update([gap], "https://x.com/p")        # streak 1
        t.update([], "https://x.com/p")            # dropped
        t.update([gap], "https://x.com/p")        # fresh again
        assert t.streak_for(gap) == 1

    def test_url_change_clears_state(self):
        t = GapTracker()
        gap = {"role": "button", "label": "OK"}
        t.update([gap], "https://x.com/a")
        t.update([gap], "https://x.com/a")
        assert t.streak_for(gap) == 2
        # Navigate to a different page — counters wiped.
        t.update([gap], "https://y.com/b")
        assert t.streak_for(gap) == 1

    def test_normalized_url_collides_on_id_segments(self):
        """Numeric IDs inside URLs shouldn't count as a different page —
        the fingerprint normalization collapses them to ``:id``."""
        t = GapTracker()
        gap = {"role": "button", "label": "OK"}
        t.update([gap], "https://shop.x.com/product/123")
        t.update([gap], "https://shop.x.com/product/456")
        assert t.streak_for(gap) == 2

    def test_reset_clears_state(self):
        t = GapTracker()
        t.update([{"role": "button", "label": "x"}], "https://x.com/a")
        t.reset()
        assert t.snapshot() == {}

    def test_malformed_entries_ignored(self):
        t = GapTracker()
        t.update(["junk", None, {"role": "button", "label": "OK"}], "https://x.com/p")
        assert t.streak_for({"role": "button", "label": "OK"}) == 1
        assert len(t.snapshot()) == 1


# ----------------------------------------------------------------------
# GapTracker.annotate — read-only streak lookup
# ----------------------------------------------------------------------


class TestGapTrackerAnnotate:
    def test_annotate_attaches_streak(self):
        t = GapTracker()
        gaps = [{"role": "button", "label": "OK"}]
        t.update(gaps, "https://x.com/p")
        t.update(gaps, "https://x.com/p")
        annotated = t.annotate(gaps)
        assert annotated[0]["streak"] == 2
        assert annotated[0]["role"] == "button"
        assert annotated[0]["label"] == "OK"

    def test_annotate_unknown_gap_defaults_to_one(self):
        t = GapTracker()
        annotated = t.annotate([{"role": "button", "label": "new"}])
        assert annotated[0]["streak"] == 1

    def test_annotate_does_not_mutate_input(self):
        t = GapTracker()
        gaps = [{"role": "button", "label": "OK"}]
        t.update(gaps, "https://x.com/p")
        original_copy = dict(gaps[0])
        t.annotate(gaps)
        assert gaps[0] == original_copy  # input untouched

    def test_cross_tier_label_variants_share_streak(self):
        """HIGH-tier vision may paraphrase labels. Token-sort normalization
        ensures the streak doesn't reset when wording changes slightly."""
        t = GapTracker()
        # Round 1 — default-tier wording.
        t.update(
            [{"role": "button", "label": "I agree to terms"}],
            "https://x.com/login",
        )
        # Round 2 — HIGH-tier rephrases it but same tokens present.
        t.update(
            [{"role": "button", "label": "terms I agree to"}],
            "https://x.com/login",
        )
        annotated = t.annotate(
            [{"role": "button", "label": "terms I agree to"}]
        )
        assert annotated[0]["streak"] == 2

    def test_annotate_skips_non_dict(self):
        t = GapTracker()
        annotated = t.annotate(["junk", None, {"role": "button", "label": "OK"}])
        assert len(annotated) == 1
        assert annotated[0]["streak"] == 1


# ----------------------------------------------------------------------
# format_gaps_for_prompt streak rendering (extension of prior tests)
# ----------------------------------------------------------------------


class TestFormatGapsWithStreak:
    def test_streak_one_renders_without_suffix(self):
        from utils.perception_gap import format_gaps_for_prompt
        out = format_gaps_for_prompt([{"role": "button", "label": "OK", "streak": 1}])
        assert "(seen" not in out
        assert "rounds" not in out

    def test_streak_two_renders_with_suffix(self):
        from utils.perception_gap import format_gaps_for_prompt
        out = format_gaps_for_prompt([{"role": "button", "label": "OK", "streak": 2}])
        assert "(seen 2 rounds)" in out
        # Multi-round guidance appears once we have any streak ≥ 2.
        assert "persisted across rounds" in out

    def test_streak_three_renders(self):
        from utils.perception_gap import format_gaps_for_prompt
        out = format_gaps_for_prompt([{"role": "checkbox", "label": "agree", "streak": 3}])
        assert "(seen 3 rounds)" in out

    def test_invalid_streak_defaults_to_single_round(self):
        from utils.perception_gap import format_gaps_for_prompt
        out = format_gaps_for_prompt([
            {"role": "button", "label": "OK", "streak": "not a number"},
        ])
        assert "(seen" not in out

    def test_missing_streak_defaults_to_single_round(self):
        # Backward compat: pre-tracker gaps have no streak field.
        from utils.perception_gap import format_gaps_for_prompt
        out = format_gaps_for_prompt([{"role": "button", "label": "OK"}])
        assert "(seen" not in out
        # Base rendering still works.
        assert "[button]" in out

    def test_mixed_streaks_annotate_correctly(self):
        from utils.perception_gap import format_gaps_for_prompt
        out = format_gaps_for_prompt([
            {"role": "button", "label": "one-shot", "streak": 1},
            {"role": "checkbox", "label": "persistent", "streak": 5},
        ])
        assert "one-shot" in out
        assert "persistent" in out
        assert "(seen 5 rounds)" in out
        # Only the persistent one gets the suffix.
        assert out.count("(seen") == 1
