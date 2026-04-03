"""
Compressed context re-injection (S2-3).

After AutoCompactor successfully compresses the conversation history,
this module rebuilds critical context that would otherwise be lost:

1. Current plan state (from plan_manager)
2. Recent artifact references
3. Active tool declarations summary
4. Session memory (from R7)

Each component is individually budget-capped to prevent the re-injection
itself from blowing the context window.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

logger = logging.getLogger("omnicore.context_reinject")

# Per-component token budgets (character approximation: 1 token ~ 4 chars)
_PLAN_MAX_CHARS = 4000       # ~1000 tokens
_ARTIFACTS_MAX_CHARS = 2000  # ~500 tokens
_TOOLS_MAX_CHARS = 2000      # ~500 tokens
_MEMORY_MAX_CHARS = 2000     # ~500 tokens


def build_reinject_messages(state: dict) -> list:
    """
    Build a list of SystemMessages to re-inject after context compaction.

    Args:
        state: The current OmniCoreState dict.

    Returns:
        List of LangChain SystemMessages (may be empty).
    """
    from langchain_core.messages import SystemMessage

    parts: List[str] = []

    # 1. Plan state
    plan_text = _extract_plan_state(state)
    if plan_text:
        parts.append(f"## 当前计划状态\n{plan_text}")

    # 2. Artifact references
    artifact_text = _extract_artifact_refs(state)
    if artifact_text:
        parts.append(f"## 最近产出物\n{artifact_text}")

    # 3. Tool declarations summary
    tool_text = _extract_tool_summary()
    if tool_text:
        parts.append(f"## 可用工具\n{tool_text}")

    # 4. Session memory
    memory_text = _extract_session_memory(state)
    if memory_text:
        parts.append(f"## 工作记忆\n{memory_text}")

    if not parts:
        return []

    combined = "\n\n".join(parts)
    msg = SystemMessage(content=f"[上下文恢复] 以下是压缩后自动恢复的关键上下文：\n\n{combined}")
    logger.debug(f"Reinject context: {len(combined)} chars, {len(parts)} sections")
    return [msg]


# ---------------------------------------------------------------------------
# Component extractors
# ---------------------------------------------------------------------------

def _extract_plan_state(state: dict) -> str:
    """Extract current plan status summary."""
    try:
        job_id = state.get("job_id", "")
        if not job_id:
            return ""
        from core.plan_manager import load_plan
        plan_content = load_plan(job_id)
        if not plan_content:
            return ""
        # Truncate to budget
        if len(plan_content) > _PLAN_MAX_CHARS:
            plan_content = plan_content[:_PLAN_MAX_CHARS] + "\n[...plan truncated]"
        return plan_content
    except Exception as exc:
        logger.debug(f"Failed to extract plan state: {exc}")
        return ""


def _extract_artifact_refs(state: dict) -> str:
    """Extract a concise list of recent artifacts."""
    try:
        task_queue = state.get("task_queue") or []
        artifacts = []
        for task in task_queue:
            if not isinstance(task, dict):
                continue
            result = task.get("result")
            if not isinstance(result, dict):
                continue

            # Collect file paths and key outputs
            for key in ("file_path", "path", "output_file", "download_path"):
                val = result.get(key)
                if val:
                    desc = str(task.get("description", ""))[:60]
                    artifacts.append(f"- {val} ({desc})")

            # Also check nested artifacts list
            for art in result.get("artifacts", []):
                if isinstance(art, dict):
                    name = art.get("name", art.get("path", ""))
                    if name:
                        artifacts.append(f"- {name}")

        if not artifacts:
            return ""

        text = "\n".join(artifacts[:20])  # cap at 20 entries
        if len(text) > _ARTIFACTS_MAX_CHARS:
            text = text[:_ARTIFACTS_MAX_CHARS] + "\n[...truncated]"
        return text
    except Exception as exc:
        logger.debug(f"Failed to extract artifact refs: {exc}")
        return ""


def _extract_tool_summary() -> str:
    """Extract a brief summary of available tools."""
    try:
        from core.tool_adapters import get_adapter_registry
        registry = get_adapter_registry()
        tools = registry.list_tools()
        if not tools:
            return ""
        lines = []
        for tool in tools:
            spec = tool.spec
            line = f"- {spec.name}: {(spec.description or '')[:80]}"
            lines.append(line)
        text = "\n".join(lines)
        if len(text) > _TOOLS_MAX_CHARS:
            text = text[:_TOOLS_MAX_CHARS] + "\n[...truncated]"
        return text
    except Exception as exc:
        logger.debug(f"Failed to extract tool summary: {exc}")
        return ""


def _extract_session_memory(state: dict) -> str:
    """Extract session memory content."""
    try:
        from config.settings import settings
        if not settings.SESSION_MEMORY_ENABLED:
            return ""
        session_id = state.get("session_id", "")
        if not session_id:
            return ""
        from core.session_memory import SessionMemoryManager
        text = SessionMemoryManager(session_id).load()
        if not text:
            return ""
        if len(text) > _MEMORY_MAX_CHARS:
            text = text[:_MEMORY_MAX_CHARS] + "\n[...truncated]"
        return text
    except Exception as exc:
        logger.debug(f"Failed to extract session memory: {exc}")
        return ""
