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


# ===========================================================================
# S2: ContextBudget
# ===========================================================================

from utils.context_budget import ContextBudget, AutoCompactor, SlotUsage
from utils.context_budget import (
    SLOT_SYSTEM_PROMPT, SLOT_MEMORY, SLOT_HISTORY, SLOT_TOOL_RESULTS,
)


class TestContextBudget:
    """Unit tests for ContextBudget (S2-1)."""

    def test_init_with_explicit_total(self):
        budget = ContextBudget(total_tokens=100000, reserve_tokens=20000)
        assert budget.total_tokens == 100000
        assert budget.reserve_tokens == 20000
        assert budget.effective_tokens == 80000

    def test_slot_allocation_ratios(self):
        budget = ContextBudget(total_tokens=100000, reserve_tokens=0)
        # Default ratios: system_prompt=15%, memory=10%, history=55%, tool_results=20%
        assert budget.get_slot_budget(SLOT_SYSTEM_PROMPT) == 15000
        assert budget.get_slot_budget(SLOT_MEMORY) == 10000
        assert budget.get_slot_budget(SLOT_HISTORY) == 55000
        assert budget.get_slot_budget(SLOT_TOOL_RESULTS) == 20000

    def test_custom_ratios(self):
        budget = ContextBudget(
            total_tokens=100000, reserve_tokens=0,
            slot_ratios={"history": 0.8, "system_prompt": 0.2},
        )
        assert budget.get_slot_budget("history") == 80000
        assert budget.get_slot_budget("system_prompt") == 20000

    def test_update_and_check_usage(self):
        budget = ContextBudget(total_tokens=100000, reserve_tokens=0)
        budget.update_usage(SLOT_HISTORY, 40000)
        budget.update_usage(SLOT_SYSTEM_PROMPT, 10000)
        report = budget.check_usage()

        assert report[SLOT_HISTORY]["used"] == 40000
        assert report[SLOT_SYSTEM_PROMPT]["used"] == 10000
        assert report["_total"]["total_used"] == 50000

    def test_should_compact_below_threshold(self):
        budget = ContextBudget(total_tokens=100000, reserve_tokens=0)
        budget.update_usage(SLOT_HISTORY, 50000)
        assert not budget.should_compact(threshold=0.85)

    def test_should_compact_above_threshold(self):
        budget = ContextBudget(total_tokens=100000, reserve_tokens=0)
        budget.update_usage(SLOT_HISTORY, 86000)
        assert budget.should_compact(threshold=0.85)

    def test_should_compact_exact_threshold(self):
        budget = ContextBudget(total_tokens=100000, reserve_tokens=0)
        budget.update_usage(SLOT_HISTORY, 85000)
        assert budget.should_compact(threshold=0.85)

    def test_total_used(self):
        budget = ContextBudget(total_tokens=100000, reserve_tokens=0)
        budget.update_usage(SLOT_HISTORY, 10000)
        budget.update_usage(SLOT_MEMORY, 5000)
        assert budget.total_used() == 15000

    def test_reserve_reduces_effective(self):
        budget = ContextBudget(total_tokens=64000, reserve_tokens=20000)
        assert budget.effective_tokens == 44000
        # Slots are allocated from effective, not total
        total_budget = sum(
            budget.get_slot_budget(s) for s in
            [SLOT_SYSTEM_PROMPT, SLOT_MEMORY, SLOT_HISTORY, SLOT_TOOL_RESULTS]
        )
        assert total_budget <= budget.effective_tokens

    def test_unknown_slot_returns_zero(self):
        budget = ContextBudget(total_tokens=100000, reserve_tokens=0)
        assert budget.get_slot_budget("nonexistent") == 0

    def test_zero_effective_tokens_should_compact_false(self):
        budget = ContextBudget(total_tokens=10000, reserve_tokens=10000)
        assert budget.effective_tokens == 0
        assert not budget.should_compact()

    def test_log_report_does_not_raise(self):
        budget = ContextBudget(total_tokens=100000, reserve_tokens=20000)
        budget.update_usage(SLOT_HISTORY, 30000)
        budget.log_report()  # should not raise


class TestSlotUsage:
    def test_remaining(self):
        s = SlotUsage(budget=1000, used=600)
        assert s.remaining == 400

    def test_remaining_over_budget(self):
        s = SlotUsage(budget=1000, used=1200)
        assert s.remaining == 0

    def test_utilization(self):
        s = SlotUsage(budget=1000, used=500)
        assert s.utilization == 0.5

    def test_utilization_zero_budget(self):
        s = SlotUsage(budget=0, used=100)
        assert s.utilization == 0.0


# ===========================================================================
# S2: AutoCompactor
# ===========================================================================

class TestAutoCompactor:
    """Unit tests for AutoCompactor (S2-2)."""

    def test_compact_too_few_messages(self):
        """Should return messages unchanged if <= 4."""
        compactor = AutoCompactor(max_consecutive_failures=3)
        msgs = _make_messages(3)
        result = compactor.compact(msgs)
        assert result is msgs

    def test_circuit_breaker_trips_after_max_failures(self):
        compactor = AutoCompactor(max_consecutive_failures=2)
        assert not compactor.is_tripped

        # Force failures by mocking _do_llm_compact
        original = compactor._do_llm_compact
        compactor._do_llm_compact = lambda msgs: (_ for _ in ()).throw(
            RuntimeError("mock failure")
        )

        msgs = _make_messages(10)
        compactor.compact(msgs)
        assert compactor.consecutive_failures == 1
        assert not compactor.is_tripped

        compactor.compact(msgs)
        assert compactor.consecutive_failures == 2
        assert compactor.is_tripped

        # After tripping, compact should return messages unchanged
        result = compactor.compact(msgs)
        assert result is msgs

    def test_reset_clears_circuit_breaker(self):
        compactor = AutoCompactor(max_consecutive_failures=1)
        compactor._do_llm_compact = lambda msgs: (_ for _ in ()).throw(
            RuntimeError("fail")
        )
        compactor.compact(_make_messages(10))
        assert compactor.is_tripped

        compactor.reset()
        assert not compactor.is_tripped
        assert compactor.consecutive_failures == 0

    def test_ptl_fallback_truncates_old_messages(self):
        compactor = AutoCompactor(max_consecutive_failures=3)
        msgs = _make_messages(10)
        result = compactor._ptl_fallback(msgs)
        # Old messages (before last 6) should have been truncated
        for msg in result[:4]:  # first 4 are old
            assert len(msg.content) < 500 or "PTL" in msg.content

    def test_ptl_fallback_drops_oldest_when_many_messages(self):
        compactor = AutoCompactor(max_consecutive_failures=3)
        msgs = _make_messages(25)
        result = compactor._ptl_fallback(msgs)
        # Should have dropped ~20% of 25 = 5 messages, replaced with summary
        assert len(result) < len(msgs)
        assert "PTL" in result[0].content
