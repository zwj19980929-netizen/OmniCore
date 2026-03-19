"""
Unified helpers for scoped memory retrieval and persistence.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from utils.text import sanitize_text, sanitize_value

if TYPE_CHECKING:
    from memory.scoped_chroma_store import ChromaMemory


_SCOPE_KEYS = ("session_id", "goal_id", "project_id", "todo_id")


def build_memory_scope(
    *,
    session_id: str = "",
    goal_id: str = "",
    project_id: str = "",
    todo_id: str = "",
) -> Dict[str, str]:
    scope: Dict[str, str] = {}
    for key, value in (
        ("session_id", session_id),
        ("goal_id", goal_id),
        ("project_id", project_id),
        ("todo_id", todo_id),
    ):
        cleaned = sanitize_text(value or "")
        if cleaned:
            scope[key] = cleaned
    return scope


def _scope_cache_key(scope: Optional[Dict[str, Any]]) -> str:
    normalized = build_memory_scope(
        session_id=str((scope or {}).get("session_id", "") or ""),
        goal_id=str((scope or {}).get("goal_id", "") or ""),
        project_id=str((scope or {}).get("project_id", "") or ""),
        todo_id=str((scope or {}).get("todo_id", "") or ""),
    )
    if not normalized:
        return "global"
    return "|".join(f"{key}:{value}" for key, value in normalized.items())


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return sanitize_text(value)
    try:
        return sanitize_text(json.dumps(sanitize_value(value), ensure_ascii=False, sort_keys=True))
    except TypeError:
        return sanitize_text(str(value))


def _extract_tool_sequence(tasks: List[Dict[str, Any]]) -> List[str]:
    tools: List[str] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        tool_name = sanitize_text(task.get("tool_name") or task.get("task_type") or "")
        if tool_name:
            tools.append(tool_name)
    return tools


def _extract_task_urls(tasks: List[Dict[str, Any]]) -> List[str]:
    urls: List[str] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        params = task.get("params") if isinstance(task.get("params"), dict) else {}
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        for key in ("url", "start_url"):
            candidate = sanitize_text(params.get(key) or "")
            if candidate and candidate not in urls:
                urls.append(candidate)
        for key in ("url", "current_url", "final_url"):
            candidate = sanitize_text(result.get(key) or "")
            if candidate and candidate not in urls:
                urls.append(candidate)
    return urls[:10]


def _extract_artifact_refs(artifacts: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    refs: List[Dict[str, str]] = []
    for artifact in artifacts or []:
        if not isinstance(artifact, dict):
            continue
        refs.append(
            {
                "artifact_id": sanitize_text(artifact.get("artifact_id") or ""),
                "artifact_type": sanitize_text(artifact.get("artifact_type") or ""),
                "name": sanitize_text(artifact.get("name") or ""),
                "path": sanitize_text(artifact.get("path") or ""),
                "preview": sanitize_text(artifact.get("preview") or ""),
            }
        )
    return refs[:10]


class MemoryManager:
    def __init__(self, chroma_memory: Optional["ChromaMemory"] = None):
        self.chroma_memory = chroma_memory

    def search_related_history(
        self,
        query: str,
        *,
        scope: Optional[Dict[str, Any]] = None,
        n_results: int = 3,
    ) -> List[Dict[str, Any]]:
        if self.chroma_memory is None:
            return []
        return sanitize_value(
            self.chroma_memory.search_memory(
                query,
                n_results=n_results,
                scope=scope,
                include_global_fallback=True,
                include_legacy_unscoped=True,
            )
        )

    def persist_preferences(
        self,
        preferences: Dict[str, Any],
        *,
        session_id: str = "",
    ) -> List[str]:
        if self.chroma_memory is None:
            return []

        scope = build_memory_scope(session_id=session_id)
        memory_ids: List[str] = []
        for key, value in (preferences or {}).items():
            cleaned_key = sanitize_text(key or "")
            if not cleaned_key:
                continue
            rendered_value = _json_text(value)
            if not rendered_value:
                continue
            memory_id = self.chroma_memory.save_user_preference(
                cleaned_key,
                rendered_value,
                scope=scope,
                metadata={"source": "runtime_preferences"},
            )
            if memory_id:
                memory_ids.append(memory_id)
        return memory_ids

    def persist_job_outcome(
        self,
        *,
        user_input: str,
        success: bool,
        final_output: str,
        final_error: str,
        intent: str,
        scope: Optional[Dict[str, Any]],
        tasks: List[Dict[str, Any]],
        artifacts: List[Dict[str, Any]],
        is_special_command: bool = False,
    ) -> Dict[str, List[str]]:
        if self.chroma_memory is None or is_special_command:
            return {"task_memories": [], "artifact_memories": []}

        cleaned_input = sanitize_text(user_input or "")
        cleaned_output = sanitize_text(final_output or "")
        cleaned_error = sanitize_text(final_error or "")
        normalized_scope = build_memory_scope(
            session_id=str((scope or {}).get("session_id", "") or ""),
            goal_id=str((scope or {}).get("goal_id", "") or ""),
            project_id=str((scope or {}).get("project_id", "") or ""),
            todo_id=str((scope or {}).get("todo_id", "") or ""),
        )
        tool_sequence = _extract_tool_sequence(tasks)
        visited_urls = _extract_task_urls(tasks)
        artifact_refs = _extract_artifact_refs(artifacts)

        created: Dict[str, List[str]] = {
            "task_memories": [],
            "artifact_memories": [],
        }
        summary_text = cleaned_output or cleaned_error
        if cleaned_input and summary_text:
            fingerprint_payload = {
                "scope": _scope_cache_key(normalized_scope),
                "task": cleaned_input,
                "success": bool(success),
                "summary": summary_text[:400],
            }
            memory_id = self.chroma_memory.save_task_result(
                task_description=cleaned_input,
                result=summary_text,
                success=success,
                scope=normalized_scope,
                metadata={
                    "intent": sanitize_text(intent or ""),
                    "tool_sequence": tool_sequence,
                    "visited_urls": visited_urls,
                    "artifact_refs": artifact_refs[:5],
                },
                fingerprint=hashlib.sha1(
                    json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()[:24],
            )
            if memory_id:
                created["task_memories"].append(memory_id)

        for artifact in artifact_refs[:5]:
            label = artifact.get("name") or artifact.get("path") or artifact.get("preview")
            if not label:
                continue
            content = f"Artifact available: {label}"
            location = artifact.get("path") or artifact.get("preview")
            if location:
                content += f"\nLocation: {location}"
            fingerprint = artifact.get("path") or artifact.get("artifact_id") or label
            memory_id = self.chroma_memory.add_memory(
                content=content,
                metadata={
                    "artifact_id": artifact.get("artifact_id", ""),
                    "artifact_type": artifact.get("artifact_type", ""),
                    "artifact_name": artifact.get("name", ""),
                    "artifact_path": artifact.get("path", ""),
                    "artifact_preview": artifact.get("preview", ""),
                    "intent": sanitize_text(intent or ""),
                },
                memory_type="artifact_reference",
                scope=normalized_scope,
                fingerprint=f"artifact:{_scope_cache_key(normalized_scope)}:{fingerprint}",
                allow_update=True,
            )
            if memory_id:
                created["artifact_memories"].append(memory_id)

        return created
