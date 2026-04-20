"""Unit tests for F2: result_sanitizer — page_main_text pollution filter."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils.result_sanitizer import sanitize_browser_data

_MAX = 800


class TestSanitizeBrowserData:
    def test_drops_fallback_when_structured_exists(self):
        data = [
            {"title": "Hermes Agent", "url": "https://github.com/x", "source": "cards"},
            {"text": "A" * 1000, "source": "page_main_text"},
            {"text": "B" * 500, "source": "page_main_text_fallback"},
        ]
        result = sanitize_browser_data(data, max_fallback_len=_MAX)
        sources = [d.get("source") for d in result]
        assert "page_main_text" not in sources
        assert "page_main_text_fallback" not in sources
        assert any(s == "cards" for s in sources)

    def test_keeps_and_truncates_fallback_only(self):
        data = [{"text": "X" * 2000, "source": "page_main_text"}]
        result = sanitize_browser_data(data, max_fallback_len=_MAX)
        assert len(result) == 1
        assert len(result[0]["text"]) == _MAX
        assert result[0].get("truncated") is True

    def test_short_fallback_not_truncated(self):
        data = [{"text": "short", "source": "page_main_text_fallback"}]
        result = sanitize_browser_data(data, max_fallback_len=_MAX)
        assert result[0]["text"] == "short"
        assert not result[0].get("truncated")

    def test_empty_list_returned_unchanged(self):
        assert sanitize_browser_data([]) == []

    def test_none_returned_unchanged(self):
        assert sanitize_browser_data(None) is None

    def test_non_list_returned_unchanged(self):
        assert sanitize_browser_data("plain string") == "plain string"

    def test_non_dict_items_survive(self):
        data = ["just a string", {"text": "fallback", "source": "page_main_text"}]
        result = sanitize_browser_data(data, max_fallback_len=_MAX)
        # non-dict items don't count as structured, so fallback is kept (only-fallback path)
        assert "just a string" in result
        assert any(
            isinstance(d, dict) and d.get("source") == "page_main_text"
            for d in result
        )

    def test_all_structured_passes_through_unchanged(self):
        data = [
            {"title": "A", "url": "http://a.com", "source": "serp"},
            {"title": "B", "url": "http://b.com", "source": "cards"},
        ]
        result = sanitize_browser_data(data)
        assert result == data
