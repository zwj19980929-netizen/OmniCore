"""
Unit tests for utils/context_budget.py (R1: 上下文成本控制)
"""
import pytest

from utils.context_budget import truncate_tool_result, truncate_result_dict, snip_history


# ---------------------------------------------------------------------------
# truncate_tool_result
# ---------------------------------------------------------------------------

def test_truncate_tool_result_short_text_unchanged():
    text = "hello world"
    assert truncate_tool_result(text, max_chars=8000) is text


def test_truncate_tool_result_exact_limit_unchanged():
    text = "x" * 8000
    assert truncate_tool_result(text, max_chars=8000) is text


def test_truncate_tool_result_long_text_truncated():
    # 10000 chars > 8000 default
    text = "A" * 5000 + "B" * 5000
    result = truncate_tool_result(text, max_chars=8000)

    assert len(result) < len(text)
    assert "[...truncated" in result
    # Head (60% of 8000 = 4800 A's) preserved
    assert result.startswith("A" * 4800)
    # Tail (30% of 8000 = 2400 B's) preserved
    assert result.endswith("B" * 2400)


def test_truncate_tool_result_omitted_count_accurate():
    text = "H" * 6000 + "T" * 6000  # 12000 chars, max=8000
    result = truncate_tool_result(text, max_chars=8000)
    # head=4800, tail=2400, omitted=12000-4800-2400=4800
    assert "[...truncated 4800 chars...]" in result


def test_truncate_tool_result_empty_string():
    assert truncate_tool_result("", max_chars=8000) == ""


def test_truncate_tool_result_none_safe():
    # None is falsy, should return as-is
    assert truncate_tool_result(None, max_chars=8000) is None  # type: ignore


def test_truncate_tool_result_respects_custom_max():
    text = "x" * 1000
    result = truncate_tool_result(text, max_chars=500)
    assert "[...truncated" in result
    # head=300, tail=150, total kept=450 + marker
    assert result.startswith("x" * 300)
    assert result.endswith("x" * 150)


# ---------------------------------------------------------------------------
# truncate_result_dict
# ---------------------------------------------------------------------------

def test_truncate_result_dict_truncates_content_field():
    result = {"content": "x" * 10000, "url": "https://example.com", "success": True}
    out = truncate_result_dict(result, max_chars=8000)
    assert "[...truncated" in out["content"]
    assert out["url"] == "https://example.com"
    assert out["success"] is True


def test_truncate_result_dict_no_change_if_short():
    result = {"content": "short", "output": "also short"}
    out = truncate_result_dict(result, max_chars=8000)
    assert out is result  # same object, no copy needed


def test_truncate_result_dict_non_dict_passthrough():
    assert truncate_result_dict("plain string", max_chars=8000) == "plain string"  # type: ignore
    assert truncate_result_dict(None, max_chars=8000) is None  # type: ignore


def test_truncate_result_dict_truncates_stdout():
    result = {"stdout": "L" * 10000, "returncode": 0}
    out = truncate_result_dict(result, max_chars=8000)
    assert "[...truncated" in out["stdout"]
    assert out["returncode"] == 0


# ---------------------------------------------------------------------------
# snip_history
# ---------------------------------------------------------------------------

def _make_messages(n: int):
    """Create n fake LangChain-like SystemMessage objects."""
    from langchain_core.messages import SystemMessage
    return [SystemMessage(content=f"Message {i}: " + "x" * 300) for i in range(n)]


def test_snip_history_no_change_when_under_threshold():
    msgs = _make_messages(10)
    result = snip_history(msgs, max_messages=20, keep_recent=10)
    assert result is msgs  # unchanged


def test_snip_history_truncates_older_messages():
    msgs = _make_messages(25)  # 25 > 20 threshold
    result = snip_history(msgs, max_messages=20, keep_recent=10)

    assert len(result) == 25
    # Last 10 messages should be intact
    for msg in result[-10:]:
        assert len(msg.content) > 200

    # Older messages should be truncated
    for msg in result[:-10]:
        assert len(msg.content) <= 213  # 200 + len("...(snipped)")
        assert msg.content.endswith("...(snipped)")


def test_snip_history_preserves_short_older_messages():
    from langchain_core.messages import SystemMessage
    short_msg = SystemMessage(content="short")
    long_msgs = _make_messages(20)
    msgs = [short_msg] + long_msgs  # 21 total, threshold=20

    result = snip_history(msgs, max_messages=20, keep_recent=10)
    # short_msg is old but <= 200 chars, should be unchanged
    assert result[0].content == "short"


def test_snip_history_empty_list():
    assert snip_history([], max_messages=20, keep_recent=10) == []


def test_snip_history_mixed_message_types():
    from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
    msgs = (
        [HumanMessage(content="H" * 300) for _ in range(8)]
        + [AIMessage(content="A" * 300) for _ in range(8)]
        + [SystemMessage(content="S" * 300) for _ in range(8)]
    )  # 24 > 20
    result = snip_history(msgs, max_messages=20, keep_recent=10)
    # Older 14 should be truncated, recent 10 intact
    for msg in result[-10:]:
        assert len(msg.content) > 200
    for msg in result[:-10]:
        assert msg.content.endswith("...(snipped)")


def test_snip_history_uses_settings_defaults():
    """snip_history with no explicit params should read from settings."""
    from config.settings import settings
    msgs = _make_messages(settings.HISTORY_MAX_MESSAGES + 5)
    result = snip_history(msgs)  # no explicit params
    # Some older messages should have been truncated
    truncated_count = sum(1 for m in result if m.content.endswith("...(snipped)"))
    assert truncated_count > 0
