"""
Unit tests for perception-gap detection and its plumbing.

Covers:
  * ``utils.perception_gap.find_perception_gaps`` — purely functional
    consistency check between vision-reported controls and DOM-extracted
    elements.
  * ``utils.vision_cache.VisionCache`` — round-trip of the new
    ``controls`` column plus legacy-schema backward compat.
  * ``agents.browser_perception.BrowserPerceptionLayer._parse_vision_response``
    — tolerant parsing of the vision LLM response (strict JSON, fenced
    block, free prose fallback).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.browser_perception import BrowserPerceptionLayer
from utils.perception_gap import (
    find_perception_gaps,
    format_gaps_for_log,
    format_gaps_for_prompt,
)
from utils.vision_cache import VisionCache, reset_singleton_for_tests


# ----------------------------------------------------------------------
# find_perception_gaps
# ----------------------------------------------------------------------


class TestFindPerceptionGaps:
    def test_empty_vision_returns_empty(self):
        assert find_perception_gaps([], [{"role": "button", "text": "Login"}]) == []

    def test_empty_elements_returns_all_controls_as_gaps(self):
        controls = [{"role": "checkbox", "label": "I agree"}]
        assert find_perception_gaps(controls, []) == controls

    def test_direct_role_label_match_has_no_gap(self):
        controls = [{"role": "button", "label": "Sign in"}]
        elements = [
            {"role": "button", "tag": "button", "text": "Sign in", "selector": "#a"},
        ]
        assert find_perception_gaps(controls, elements) == []

    def test_role_equivalence_button_submit(self):
        # HTML button[type=submit] maps to role "button"
        controls = [{"role": "button", "label": "Log in"}]
        elements = [
            {"role": "submit", "tag": "input", "type": "submit",
             "value": "Log in", "selector": "#b"},
        ]
        assert find_perception_gaps(controls, elements) == []

    def test_role_mismatch_produces_gap(self):
        # Vision says checkbox, DOM has button — label matches but role incompatible
        controls = [{"role": "checkbox", "label": "I agree to terms"}]
        elements = [
            {"role": "button", "tag": "button", "text": "I agree to terms",
             "selector": "#c"},
        ]
        gaps = find_perception_gaps(controls, elements)
        assert len(gaps) == 1
        assert gaps[0]["label"] == "I agree to terms"

    def test_label_overlap_via_tokens(self):
        controls = [{"role": "link", "label": "forgot your password"}]
        elements = [
            {"role": "link", "tag": "a", "text": "Forgot password?",
             "selector": "#d"},
        ]
        assert find_perception_gaps(controls, elements) == []

    def test_label_substring_containment(self):
        controls = [{"role": "button", "label": "Login"}]
        elements = [
            {"role": "button", "tag": "button", "text": "Login with Google",
             "selector": "#e"},
        ]
        assert find_perception_gaps(controls, elements) == []

    def test_checkbox_via_label_text(self):
        # Simulates a hidden input[type=checkbox] paired with visible label —
        # the label text is what the vision model reads.
        controls = [{"role": "checkbox", "label": "我已阅读并同意《平台隐私协议》"}]
        elements = [
            {"role": "checkbox", "tag": "label", "text": "我已阅读并同意《平台隐私协议》",
             "selector": "label[for=agree]"},
        ]
        assert find_perception_gaps(controls, elements) == []

    def test_missing_checkbox_is_reported_as_gap(self):
        # The failure mode from the user's log: vision sees a protocol
        # checkbox, DOM extraction produced only username/password/login.
        controls = [
            {"role": "input", "label": "Username"},
            {"role": "input", "label": "Password"},
            {"role": "checkbox", "label": "I agree to the privacy policy"},
            {"role": "button", "label": "Log in"},
        ]
        elements = [
            {"role": "input", "tag": "input", "placeholder": "Username",
             "selector": "#u"},
            {"role": "input", "tag": "input", "type": "password",
             "placeholder": "Password", "selector": "#p"},
            {"role": "button", "tag": "button", "text": "Log in",
             "selector": "#btn"},
        ]
        gaps = find_perception_gaps(controls, elements)
        assert len(gaps) == 1
        assert "privacy" in gaps[0]["label"].lower()

    def test_empty_label_control_is_skipped(self):
        # A control with neither role nor label contributes nothing.
        controls = [{"role": "", "label": ""}]
        assert find_perception_gaps(controls, []) == []

    def test_control_without_label_matches_anything(self):
        # Without a label we can't decide — don't emit a gap.
        controls = [{"role": "button", "label": ""}]
        elements = [{"role": "button", "text": "Something", "selector": "#x"}]
        assert find_perception_gaps(controls, elements) == []

    def test_non_dict_entries_are_ignored(self):
        controls = ["not a dict", None, {"role": "button", "label": "OK"}]
        elements = ["also junk", {"role": "button", "text": "OK", "selector": "#y"}]
        assert find_perception_gaps(controls, elements) == []

    def test_ordering_is_preserved(self):
        controls = [
            {"role": "button", "label": "Alpha"},
            {"role": "button", "label": "Bravo"},
            {"role": "button", "label": "Charlie"},
        ]
        # No matches in elements — all three come back in order.
        gaps = find_perception_gaps(controls, [])
        assert [g["label"] for g in gaps] == ["Alpha", "Bravo", "Charlie"]


# ----------------------------------------------------------------------
# format_gaps_for_log
# ----------------------------------------------------------------------


class TestFormatGapsForLog:
    def test_none_when_empty(self):
        assert format_gaps_for_log([]) == "none"

    def test_single_gap(self):
        out = format_gaps_for_log([{"role": "checkbox", "label": "I agree"}])
        assert "checkbox" in out
        assert "I agree" in out

    def test_truncates_long_labels(self):
        long_label = "a" * 80
        out = format_gaps_for_log([{"role": "button", "label": long_label}])
        assert "..." in out
        assert len(out) < 80  # original label truncated

    def test_caps_at_eight_with_suffix(self):
        gaps = [{"role": "button", "label": f"g{i}"} for i in range(12)]
        out = format_gaps_for_log(gaps)
        assert "+4 more" in out


# ----------------------------------------------------------------------
# format_gaps_for_prompt
# ----------------------------------------------------------------------


class TestFormatGapsForPrompt:
    def test_empty_gaps_returns_empty_string(self):
        # Empty string is important: prompt templates include the
        # placeholder unconditionally and we don't want a stray header
        # when there's nothing to report.
        assert format_gaps_for_prompt([]) == ""

    def test_renders_role_and_label(self):
        out = format_gaps_for_prompt([{"role": "checkbox", "label": "I agree"}])
        assert "VISION-ONLY CONTROLS" in out
        assert "[checkbox]" in out
        assert "I agree" in out

    def test_neutral_guidance_no_commands(self):
        """The block must read as a hint, not a directive — otherwise the
        agent drifts toward rule-following. Check for hint-like language."""
        out = format_gaps_for_prompt([{"role": "button", "label": "Go"}])
        assert "hint" in out.lower()
        # And NOT imperative commands like "MUST" / "DO NOT" etc.
        assert "MUST" not in out
        assert "do not" not in out.lower()

    def test_mentions_possible_explanations(self):
        # Scroll / iframe / tab / label-click are all legitimate
        # resolutions and should be surfaced as possibilities.
        out = format_gaps_for_prompt([{"role": "checkbox", "label": "Agree"}])
        lowered = out.lower()
        assert "scroll" in lowered
        assert "iframe" in lowered
        assert "tab" in lowered
        assert "label" in lowered

    def test_truncates_long_labels(self):
        long_label = "x" * 200
        out = format_gaps_for_prompt([{"role": "button", "label": long_label}])
        # Truncation marker present, full label not echoed.
        assert "..." in out
        assert "x" * 200 not in out

    def test_caps_at_max_with_suffix(self):
        gaps = [{"role": "button", "label": f"g{i}"} for i in range(10)]
        out = format_gaps_for_prompt(gaps)
        assert "+4 more" in out  # 10 - 6 cap = 4

    def test_non_dict_entries_skipped(self):
        out = format_gaps_for_prompt([
            "junk",
            {"role": "button", "label": "OK"},
            None,
        ])
        assert "[button]" in out
        # No bullet line should be emitted for the junk entries
        assert out.count("\n- ") == 1

    def test_missing_role_renders_as_question_mark(self):
        out = format_gaps_for_prompt([{"label": "Something"}])
        assert "[?]" in out
        assert "Something" in out

    def test_missing_label_still_renders_role(self):
        out = format_gaps_for_prompt([{"role": "slider", "label": ""}])
        assert "[slider]" in out


# ----------------------------------------------------------------------
# VisionCache: controls round-trip + legacy schema
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_cache(monkeypatch):
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "BROWSER_VISION_CACHE_ENABLED", True)
    monkeypatch.setattr(_settings, "BROWSER_VISION_CACHE_TTL_DAYS", 7)
    monkeypatch.setattr(
        _settings,
        "BROWSER_VISION_CACHE_BYPASS_KEYWORDS",
        "login,payment",
    )
    reset_singleton_for_tests()
    yield
    reset_singleton_for_tests()


class TestVisionCacheControls:
    def test_controls_round_trip(self, tmp_path):
        cache = VisionCache(db_path=tmp_path / "vc.db")
        controls = [
            {"role": "button", "label": "Sign in"},
            {"role": "checkbox", "label": "Remember me"},
        ]
        assert cache.set("h1", "login page", "x.com/login", controls=controls) is True
        cached = cache.get("h1")
        assert cached is not None
        assert cached.controls == controls

    def test_set_without_controls_yields_empty_list(self, tmp_path):
        cache = VisionCache(db_path=tmp_path / "vc.db")
        assert cache.set("h2", "homepage", "x.com") is True
        cached = cache.get("h2")
        assert cached is not None
        assert cached.controls == []

    def test_malformed_controls_are_dropped(self, tmp_path):
        cache = VisionCache(db_path=tmp_path / "vc.db")
        controls = [
            {"role": "button", "label": "OK"},
            "not a dict",
            {"role": "", "label": ""},  # empty — dropped
            None,
        ]
        assert cache.set("h3", "desc", controls=controls) is True
        cached = cache.get("h3")
        assert cached is not None
        assert cached.controls == [{"role": "button", "label": "OK"}]

    def test_legacy_db_without_controls_column_is_migrated(self, tmp_path):
        """Opening a pre-migration DB must not lose the legacy rows and
        should add the ``controls_json`` column on the fly."""
        import sqlite3

        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE vision_cache (
                page_hash TEXT PRIMARY KEY,
                url_template TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL
            )
            """
        )
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO vision_cache VALUES (?, ?, ?, 0, ?, ?)",
            ("legacy-hash", "x.com/old", "legacy description", now, now),
        )
        conn.commit()
        conn.close()

        cache = VisionCache(db_path=db_path)
        cached = cache.get("legacy-hash")
        assert cached is not None
        assert cached.description == "legacy description"
        assert cached.controls == []  # column backfilled to empty

        # And subsequent writes with controls work too.
        assert cache.set(
            "new-hash", "fresh desc",
            controls=[{"role": "button", "label": "Go"}],
        ) is True
        refreshed = cache.get("new-hash")
        assert refreshed is not None
        assert refreshed.controls == [{"role": "button", "label": "Go"}]


# ----------------------------------------------------------------------
# BrowserPerceptionLayer._parse_vision_response
# ----------------------------------------------------------------------


class TestParseVisionResponse:
    def _resp(self, content):
        return SimpleNamespace(content=content)

    def test_strict_json(self):
        content = '{"summary": "A login page.", "interactive_controls": [{"role": "button", "label": "Log in"}]}'
        summary, controls = BrowserPerceptionLayer._parse_vision_response(self._resp(content))
        assert summary == "A login page."
        assert controls == [{"role": "button", "label": "Log in"}]

    def test_fenced_json_block(self):
        content = "Here is what I see:\n```json\n{\"summary\": \"S\", \"interactive_controls\": []}\n```"
        summary, controls = BrowserPerceptionLayer._parse_vision_response(self._resp(content))
        assert summary == "S"
        assert controls == []

    def test_free_prose_falls_back_to_summary(self):
        content = "This is a plain description with no JSON at all."
        summary, controls = BrowserPerceptionLayer._parse_vision_response(self._resp(content))
        assert summary == content
        assert controls == []

    def test_empty_response(self):
        assert BrowserPerceptionLayer._parse_vision_response(self._resp("")) == ("", [])
        assert BrowserPerceptionLayer._parse_vision_response(self._resp(None)) == ("", [])

    def test_malformed_controls_entries_filtered(self):
        content = '{"summary": "x", "interactive_controls": [{"role": "button", "label": "OK"}, "junk", {"foo": "bar"}]}'
        summary, controls = BrowserPerceptionLayer._parse_vision_response(self._resp(content))
        assert summary == "x"
        assert controls == [{"role": "button", "label": "OK"}]

    def test_summary_truncated_to_500(self):
        long_summary = "x" * 800
        content = f'{{"summary": "{long_summary}", "interactive_controls": []}}'
        summary, _ = BrowserPerceptionLayer._parse_vision_response(self._resp(content))
        assert len(summary) == 500
