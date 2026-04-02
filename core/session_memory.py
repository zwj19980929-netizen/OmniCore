"""
Session Memory — periodic working-memory extraction for long sessions (R7).

Maintains a per-session Markdown summary at ``data/sessions/{session_id}_memory.md``.
The summary is produced by a low-cost LLM call and injected into ``snip_history``
so that the model retains awareness of early decisions even after history is trimmed.
"""

import os
from datetime import datetime
from typing import List, Optional

from utils.logger import log_agent_action, log_warning


SESSIONS_DIR = os.path.join("data", "sessions")


class SessionMemoryManager:
    """Manage working memory for a single session."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._llm = None
        self._last_extract_turn: int = 0
        self._memory_path = os.path.join(SESSIONS_DIR, f"{session_id}_memory.md")

    # ------------------------------------------------------------------
    # Trigger check
    # ------------------------------------------------------------------

    def should_extract(self, turn_count: int) -> bool:
        """Return True when an extraction should be triggered."""
        from config.settings import settings

        if not settings.SESSION_MEMORY_ENABLED:
            return False
        if turn_count <= 0:
            return False
        if turn_count - self._last_extract_turn < settings.SESSION_MEMORY_INTERVAL:
            return False
        return True

    # ------------------------------------------------------------------
    # Core extract
    # ------------------------------------------------------------------

    def extract(
        self,
        messages: list,
        task_queue: Optional[list] = None,
        turn_count: int = 0,
    ) -> str:
        """Extract session memory from recent conversation and task state.

        Returns the extracted memory text (also persisted to disk).
        """
        from config.settings import settings

        existing_memory = self.load()
        prompt = self._build_extract_prompt(
            messages, task_queue or [], existing_memory, turn_count,
            window=settings.SESSION_MEMORY_INTERVAL * 2,
        )
        system_prompt = self._load_system_prompt()

        if self._llm is None:
            from core.llm import LLMClient
            self._llm = LLMClient(complexity=0.2)

        try:
            response = self._llm.chat_with_system(
                system_prompt=system_prompt,
                user_message=prompt,
                temperature=0.3,
                max_tokens=1024,
            )
            memory_text = getattr(response, "content", "") or ""
        except Exception as exc:
            log_warning(f"Session memory extraction LLM call failed: {exc}")
            return existing_memory

        if not memory_text.strip():
            return existing_memory

        self.save(memory_text, turn_count)
        self._last_extract_turn = turn_count
        log_agent_action("SessionMemory", f"提炼完成 (第 {turn_count} 轮)")

        return memory_text

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, memory_text: str, turn_count: int = 0) -> None:
        """Write session memory to disk."""
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        header = (
            f"# Session Memory — {self.session_id}\n\n"
            f"> 最后更新: {now} (第 {turn_count} 轮)\n\n"
        )
        with open(self._memory_path, "w", encoding="utf-8") as f:
            f.write(header + memory_text)

    def load(self) -> str:
        """Load existing session memory (empty string if absent)."""
        if not os.path.exists(self._memory_path):
            return ""
        try:
            with open(self._memory_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    @staticmethod
    def _load_system_prompt() -> str:
        from utils.prompt_manager import get_prompt
        fallback = (
            "你是一个工作记忆提炼助手。从对话历史和任务状态中提炼关键工作记忆。"
            "输出 Markdown，包含：当前工作状态、关键决策记录、已确认的约束、失败路径。"
        )
        return get_prompt("session_memory_extract", fallback)

    @staticmethod
    def _build_extract_prompt(
        messages: list,
        task_queue: list,
        existing_memory: str,
        turn_count: int,
        window: int = 16,
    ) -> str:
        parts: List[str] = []

        if existing_memory:
            parts.append(f"## 现有工作记忆\n\n{existing_memory}")

        recent = messages[-window:] if len(messages) > window else messages
        msg_text = _format_messages(recent)
        start_turn = max(turn_count - len(recent) + 1, 1)
        parts.append(
            f"## 最近对话（第 {start_turn}~{turn_count} 轮）\n\n{msg_text}"
        )

        if task_queue:
            parts.append(f"## 当前任务状态\n\n{_format_task_queue(task_queue)}")

        return "\n\n---\n\n".join(parts)


# ------------------------------------------------------------------
# Formatting helpers (module-level for testability)
# ------------------------------------------------------------------

def _format_messages(messages: list) -> str:
    lines: List[str] = []
    for msg in messages:
        if hasattr(msg, "type") and hasattr(msg, "content"):
            role = msg.type
            content = msg.content
        elif isinstance(msg, dict):
            role = msg.get("role", msg.get("type", "?"))
            content = msg.get("content", "")
        else:
            continue

        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in content
            )
        if isinstance(content, str) and len(content) > 500:
            content = content[:500] + "..."

        lines.append(f"[{role}] {content}")
    return "\n".join(lines)


def _format_task_queue(task_queue: list) -> str:
    lines: List[str] = []
    for task in task_queue:
        tid = task.get("task_id", "?")
        status = task.get("status", "?")
        tool = task.get("tool_name", task.get("task_type", "?"))
        desc = str(task.get("description", "") or task.get("params", {}).get("description", ""))[:80]
        lines.append(f"- [{status}] {tid}: {desc} ({tool})")
    return "\n".join(lines)
