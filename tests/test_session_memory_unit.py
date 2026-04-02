"""R7: Session Memory unit tests."""

import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from core.session_memory import SessionMemoryManager, _format_messages, _format_task_queue
from utils.context_budget import snip_history


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_sessions_dir(tmp_path):
    """Override SESSIONS_DIR to a temp path."""
    d = str(tmp_path / "sessions")
    with patch("core.session_memory.SESSIONS_DIR", d):
        yield d


@pytest.fixture
def manager(tmp_sessions_dir):
    return SessionMemoryManager("test-session-001")


# ---------------------------------------------------------------------------
# should_extract
# ---------------------------------------------------------------------------

class TestShouldExtract:

    def test_disabled_returns_false(self, manager):
        with patch("config.settings.Settings.SESSION_MEMORY_ENABLED", False):
            assert manager.should_extract(turn_count=16) is False

    def test_zero_turn_returns_false(self, manager):
        with patch("config.settings.Settings.SESSION_MEMORY_ENABLED", True):
            with patch("config.settings.Settings.SESSION_MEMORY_INTERVAL", 8):
                assert manager.should_extract(turn_count=0) is False

    def test_before_interval_returns_false(self, manager):
        with patch("config.settings.Settings.SESSION_MEMORY_ENABLED", True):
            with patch("config.settings.Settings.SESSION_MEMORY_INTERVAL", 8):
                assert manager.should_extract(turn_count=5) is False

    def test_at_interval_returns_true(self, manager):
        with patch("config.settings.Settings.SESSION_MEMORY_ENABLED", True):
            with patch("config.settings.Settings.SESSION_MEMORY_INTERVAL", 8):
                assert manager.should_extract(turn_count=8) is True

    def test_respects_last_extract_turn(self, manager):
        with patch("config.settings.Settings.SESSION_MEMORY_ENABLED", True):
            with patch("config.settings.Settings.SESSION_MEMORY_INTERVAL", 8):
                manager._last_extract_turn = 8
                assert manager.should_extract(turn_count=12) is False
                assert manager.should_extract(turn_count=16) is True


# ---------------------------------------------------------------------------
# save / load
# ---------------------------------------------------------------------------

class TestSaveLoad:

    def test_save_and_load(self, manager, tmp_sessions_dir):
        manager.save("## Test Memory\n- item1", turn_count=8)
        loaded = manager.load()
        assert "Session Memory" in loaded
        assert "第 8 轮" in loaded
        assert "## Test Memory" in loaded

    def test_load_nonexistent_returns_empty(self, manager, tmp_sessions_dir):
        assert manager.load() == ""

    def test_save_creates_directory(self, tmp_path):
        d = str(tmp_path / "deep" / "sessions")
        with patch("core.session_memory.SESSIONS_DIR", d):
            mgr = SessionMemoryManager("deep-test")
            mgr.save("content", turn_count=1)
            assert os.path.exists(os.path.join(d, "deep-test_memory.md"))


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

class TestExtract:

    def test_extract_calls_llm_and_saves(self, manager, tmp_sessions_dir):
        mock_response = MagicMock()
        mock_response.content = "## 当前工作状态\n- 测试中"

        mock_llm = MagicMock()
        mock_llm.chat_with_system.return_value = mock_response
        manager._llm = mock_llm

        messages = [
            HumanMessage(content="帮我查一下天气"),
            AIMessage(content="好的，我来查"),
        ]

        with patch("config.settings.Settings.SESSION_MEMORY_INTERVAL", 8):
            result = manager.extract(messages, task_queue=[], turn_count=8)

        assert "当前工作状态" in result
        assert mock_llm.chat_with_system.called

        # verify saved to disk
        loaded = manager.load()
        assert "当前工作状态" in loaded
        assert manager._last_extract_turn == 8

    def test_extract_llm_failure_returns_existing(self, manager, tmp_sessions_dir):
        manager.save("old memory", turn_count=4)

        mock_llm = MagicMock()
        mock_llm.chat_with_system.side_effect = RuntimeError("LLM down")
        manager._llm = mock_llm

        messages = [HumanMessage(content="test")]
        with patch("config.settings.Settings.SESSION_MEMORY_INTERVAL", 8):
            result = manager.extract(messages, turn_count=8)

        assert "old memory" in result

    def test_extract_empty_response_keeps_existing(self, manager, tmp_sessions_dir):
        manager.save("existing memory", turn_count=4)

        mock_response = MagicMock()
        mock_response.content = "   "
        mock_llm = MagicMock()
        mock_llm.chat_with_system.return_value = mock_response
        manager._llm = mock_llm

        messages = [HumanMessage(content="test")]
        with patch("config.settings.Settings.SESSION_MEMORY_INTERVAL", 8):
            result = manager.extract(messages, turn_count=8)

        assert "existing memory" in result


# ---------------------------------------------------------------------------
# _format_messages
# ---------------------------------------------------------------------------

class TestFormatMessages:

    def test_langchain_messages(self):
        msgs = [
            HumanMessage(content="Hello"),
            AIMessage(content="Hi there"),
        ]
        result = _format_messages(msgs)
        assert "[human] Hello" in result
        assert "[ai] Hi there" in result

    def test_dict_messages(self):
        msgs = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        result = _format_messages(msgs)
        assert "[user] Hello" in result
        assert "[assistant] Hi" in result

    def test_long_content_truncated(self):
        msg = HumanMessage(content="x" * 600)
        result = _format_messages([msg])
        assert "..." in result
        assert len(result) < 600

    def test_list_content_joined(self):
        msg = MagicMock()
        msg.type = "human"
        msg.content = [{"text": "part1"}, {"text": "part2"}]
        result = _format_messages([msg])
        assert "part1" in result
        assert "part2" in result


# ---------------------------------------------------------------------------
# _format_task_queue
# ---------------------------------------------------------------------------

class TestFormatTaskQueue:

    def test_basic_formatting(self):
        queue = [
            {"task_id": "t1", "status": "completed", "tool_name": "web.search",
             "description": "Search for info"},
            {"task_id": "t2", "status": "pending", "task_type": "browser_worker",
             "params": {"description": "Browse page"}},
        ]
        result = _format_task_queue(queue)
        assert "[completed] t1" in result
        assert "[pending] t2" in result
        assert "web.search" in result


# ---------------------------------------------------------------------------
# snip_history with session_memory (R7 integration in context_budget)
# ---------------------------------------------------------------------------

class TestSnipHistoryWithSessionMemory:

    def test_session_memory_prepended_when_snipping(self):
        msgs = [HumanMessage(content=f"msg {i}") for i in range(25)]
        result = snip_history(msgs, max_messages=20, keep_recent=10, session_memory="Test memory")
        assert isinstance(result[0], SystemMessage)
        assert "[工作记忆]" in result[0].content
        assert "Test memory" in result[0].content

    def test_no_session_memory_no_extra_message(self):
        msgs = [HumanMessage(content=f"msg {i}") for i in range(25)]
        result = snip_history(msgs, max_messages=20, keep_recent=10, session_memory="")
        # First message should NOT be a session memory SystemMessage
        assert not (isinstance(result[0], SystemMessage) and "[工作记忆]" in result[0].content)

    def test_session_memory_with_short_history(self):
        msgs = [HumanMessage(content="hello")]
        result = snip_history(msgs, max_messages=20, keep_recent=10, session_memory="My memory")
        assert len(result) == 2
        assert isinstance(result[0], SystemMessage)
        assert result[1].content == "hello"

    def test_session_memory_with_empty_messages(self):
        result = snip_history([], max_messages=20, keep_recent=10, session_memory="My memory")
        assert len(result) == 1
        assert isinstance(result[0], SystemMessage)

    def test_empty_session_memory_empty_messages(self):
        result = snip_history([], max_messages=20, keep_recent=10, session_memory="")
        assert result == []


# ---------------------------------------------------------------------------
# Build extract prompt
# ---------------------------------------------------------------------------

class TestBuildExtractPrompt:

    def test_includes_existing_memory(self, manager):
        prompt = SessionMemoryManager._build_extract_prompt(
            messages=[HumanMessage(content="hi")],
            task_queue=[],
            existing_memory="previous state",
            turn_count=8,
        )
        assert "现有工作记忆" in prompt
        assert "previous state" in prompt

    def test_no_existing_memory(self, manager):
        prompt = SessionMemoryManager._build_extract_prompt(
            messages=[HumanMessage(content="hi")],
            task_queue=[],
            existing_memory="",
            turn_count=8,
        )
        assert "现有工作记忆" not in prompt
        assert "最近对话" in prompt

    def test_includes_task_queue(self, manager):
        prompt = SessionMemoryManager._build_extract_prompt(
            messages=[HumanMessage(content="hi")],
            task_queue=[{"task_id": "t1", "status": "pending", "tool_name": "web.search"}],
            existing_memory="",
            turn_count=8,
        )
        assert "当前任务状态" in prompt
        assert "t1" in prompt
