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
    from memory.entity_extractor import EntityExtractor
    from memory.entity_index import EntityIndex, EntityRecord
    from memory.tiered_store import TieredMemoryStore


_SCOPE_KEYS = ("session_id", "goal_id", "project_id", "todo_id")


def _safe_json_dumps(obj: Any) -> str:
    """安全序列化为 JSON 字符串，不可序列化的对象降级为 str()。"""
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return "[]"


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
    def __init__(
        self,
        chroma_memory: Optional["ChromaMemory"] = None,
        entity_extractor: Optional["EntityExtractor"] = None,
        entity_index: Optional["EntityIndex"] = None,
        tiered_store: Optional["TieredMemoryStore"] = None,
    ):
        self.chroma_memory = chroma_memory
        self._entity_extractor = entity_extractor
        self._entity_index = entity_index
        self._tiered_store = tiered_store

    @property
    def tiered_store(self) -> Optional["TieredMemoryStore"]:
        """A4: Lazy-load TieredMemoryStore when ``MEMORY_TIERED_ENABLED``.

        When tiered is off this returns None and callers fall back to the
        legacy ``chroma_memory`` path.
        """
        from config.settings import settings as _settings
        if not _settings.MEMORY_TIERED_ENABLED:
            return None
        if self._tiered_store is None:
            try:
                from memory.tiered_store import TieredMemoryStore
                # Pass the legacy chroma_memory in as the fallback reader
                # so pre-migration data stays visible.
                self._tiered_store = TieredMemoryStore(legacy=self.chroma_memory)
            except Exception:
                pass
        return self._tiered_store

    @property
    def entity_extractor(self) -> Optional["EntityExtractor"]:
        """Lazy-load EntityExtractor on first use."""
        if self._entity_extractor is None:
            try:
                from memory.entity_extractor import EntityExtractor
                self._entity_extractor = EntityExtractor()
            except Exception:
                pass
        return self._entity_extractor

    @property
    def entity_index(self) -> Optional["EntityIndex"]:
        """Lazy-load EntityIndex; returns None when the feature is disabled."""
        from config.settings import settings as _settings
        if not _settings.MEMORY_ENTITY_INDEX_ENABLED:
            return None
        if self._entity_index is None:
            try:
                from memory.entity_index import EntityIndex
                self._entity_index = EntityIndex()
            except Exception:
                pass
        return self._entity_index

    def search_by_entity(
        self,
        entity_text: str,
        *,
        entity_type: Optional[str] = None,
        limit: int = 10,
    ) -> List["EntityRecord"]:
        index = self.entity_index
        if index is None:
            return []
        return index.search(entity_text, entity_type=entity_type, limit=limit)

    def list_top_entities(
        self,
        *,
        entity_type: Optional[str] = None,
        limit: int = 10,
    ) -> List["EntityRecord"]:
        index = self.entity_index
        if index is None:
            return []
        return index.top_entities(entity_type=entity_type, limit=limit)

    def _writer_for(self, memory_type: str) -> Optional["ChromaMemory"]:
        """A4: pick the right backing store for a write by memory_type.

        When tiered is on, routes to the tier-specific collection so new
        writes accumulate where future reads expect them; falls back to
        the legacy ``chroma_memory`` otherwise.
        """
        tiered = self.tiered_store
        if tiered is not None:
            target = tiered.store_for_type(memory_type)
            if target is not None:
                return target
        return self.chroma_memory

    def search_related_history(
        self,
        query: str,
        *,
        scope: Optional[Dict[str, Any]] = None,
        n_results: int = 3,
    ) -> List[Dict[str, Any]]:
        # A4: prefer tiered search when enabled; TieredMemoryStore merges
        # working/episodic/semantic by tier-weighted score and falls back
        # to the legacy collection when the tiers are still empty.
        tiered = self.tiered_store
        if tiered is not None:
            try:
                return sanitize_value(
                    tiered.search(
                        query,
                        scope=scope,
                        n_results=n_results,
                        include_global_fallback=True,
                    )
                )
            except Exception:
                pass  # fall through to legacy path
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

    def persist_inferred_preferences(
        self,
        candidates: List[Any],
        *,
        session_id: str = "",
    ) -> List[str]:
        """A5: write preference candidates above the confidence threshold.

        Each candidate is a ``PreferenceCandidate`` (from
        ``memory.preference_learner``). The record is marked
        ``source=inferred`` so it can be distinguished from user-supplied
        preferences and bulk-removed if the learner misbehaves.
        """
        if self.chroma_memory is None or not candidates:
            return []
        from config.settings import settings as _settings
        threshold = float(_settings.PREFERENCE_LEARNING_MIN_CONFIDENCE)
        scope = build_memory_scope(session_id=session_id)
        writer = self._writer_for("preference") or self.chroma_memory
        written: List[str] = []
        for cand in candidates:
            key = sanitize_text(str(getattr(cand, "key", "") or ""))
            value = sanitize_text(str(getattr(cand, "value", "") or ""))
            confidence = float(getattr(cand, "confidence", 0.0) or 0.0)
            if not key or not value or confidence < threshold:
                continue
            evidence_ids = getattr(cand, "evidence_ids", None) or []
            notes = sanitize_text(str(getattr(cand, "notes", "") or ""))
            metadata = {
                "source": "inferred",
                "confidence": round(confidence, 4),
                "evidence_ids": ",".join(str(x) for x in list(evidence_ids)[:10]),
                "notes": notes,
            }
            memory_id = writer.save_user_preference(
                key,
                value,
                scope=scope,
                metadata=metadata,
            )
            if memory_id:
                written.append(memory_id)
        return written

    def persist_preferences(
        self,
        preferences: Dict[str, Any],
        *,
        session_id: str = "",
    ) -> List[str]:
        if self.chroma_memory is None:
            return []

        scope = build_memory_scope(session_id=session_id)
        writer = self._writer_for("preference") or self.chroma_memory
        memory_ids: List[str] = []
        for key, value in (preferences or {}).items():
            cleaned_key = sanitize_text(key or "")
            if not cleaned_key:
                continue
            rendered_value = _json_text(value)
            if not rendered_value:
                continue
            memory_id = writer.save_user_preference(
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

            # --- Entity extraction (best-effort) ---
            entity_metadata: Dict[str, Any] = {}
            raw_entities: List[Dict[str, Any]] = []
            extractor = self.entity_extractor
            if extractor is not None:
                try:
                    extraction = extractor.extract(f"{cleaned_input}\n{summary_text[:2000]}")
                    entities = extraction.get("entities") or []
                    raw_entities = list(entities)[:20]
                    if entities:
                        # Store entity names grouped by type for metadata filtering
                        by_type: Dict[str, List[str]] = {}
                        for ent in entities[:20]:
                            etype = sanitize_text(ent.get("type") or "KEYWORD")
                            etext = sanitize_text(ent.get("text") or "")
                            if etext:
                                by_type.setdefault(etype, []).append(etext)
                        for etype, texts in by_type.items():
                            entity_metadata[f"entity_{etype.lower()}"] = ",".join(texts[:10])
                    entity_summary = sanitize_text(extraction.get("summary") or "")
                    if entity_summary:
                        entity_metadata["entity_summary"] = entity_summary[:300]
                except Exception:
                    pass  # entity extraction is best-effort

            task_metadata = {
                "intent": sanitize_text(intent or ""),
                "tool_sequence": tool_sequence,
                "visited_urls": visited_urls,
                "artifact_refs": _safe_json_dumps(artifact_refs[:5]),
            }
            task_metadata.update(entity_metadata)

            task_writer = self._writer_for("task_result") or self.chroma_memory
            memory_id = task_writer.save_task_result(
                task_description=cleaned_input,
                result=summary_text,
                success=success,
                scope=normalized_scope,
                metadata=task_metadata,
                fingerprint=hashlib.sha1(
                    json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()[:24],
            )
            if memory_id:
                created["task_memories"].append(memory_id)

            # A2: also record entities in the inverted index so we can do
            # faceted retrieval later.
            if raw_entities and memory_id:
                index = self.entity_index
                if index is not None:
                    try:
                        index.record_many(
                            raw_entities,
                            memory_id=memory_id,
                            scope=normalized_scope,
                        )
                    except Exception:
                        pass  # never block main write path

        artifact_writer = self._writer_for("artifact_reference") or self.chroma_memory
        for artifact in artifact_refs[:5]:
            label = artifact.get("name") or artifact.get("path") or artifact.get("preview")
            if not label:
                continue
            content = f"Artifact available: {label}"
            location = artifact.get("path") or artifact.get("preview")
            if location:
                content += f"\nLocation: {location}"
            fingerprint = artifact.get("path") or artifact.get("artifact_id") or label
            memory_id = artifact_writer.add_memory(
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

        # A5: opportunistic preference learning triggered after writes, gated
        # by ``PREFERENCE_LEARNING_MIN_INTERVAL_HOURS`` so it runs at most
        # once per configured window. Failures never propagate.
        if created["task_memories"] or created["artifact_memories"]:
            try:
                from memory.preference_learner import maybe_run_learner
                maybe_run_learner(self)
            except Exception:
                pass

        return created
