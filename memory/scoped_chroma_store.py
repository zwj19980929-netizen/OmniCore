"""
Scoped Chroma-backed long-term memory storage.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import chromadb
from chromadb.config import Settings as ChromaSettings

from config.settings import settings
from utils.logger import log_agent_action, log_success, logger
from utils.text import sanitize_text, sanitize_value


_SCOPE_KEYS = ("session_id", "goal_id", "project_id", "todo_id")

# Multilingual embedding model — much better Chinese support than the default
# all-MiniLM-L6-v2.  Lazy-loaded once per process on first memory operation.
_EMBEDDING_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
_embedding_fn = None


def _get_embedding_fn():
    global _embedding_fn
    if _embedding_fn is None:
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
        _embedding_fn = SentenceTransformerEmbeddingFunction(
            model_name=_EMBEDDING_MODEL_NAME,
        )
    return _embedding_fn


class ChromaMemory:
    """
    Persistent vector memory with scoped retrieval and stable upserts.

    The ChromaDB collection is lazily initialized on the first actual
    memory operation, so constructing this class does NOT block startup.
    """

    def __init__(self, collection_name: str = "omnicore_memory"):
        self.name = "ChromaMemory"
        self.collection_name = collection_name
        self._client = None
        self.__collection = None  # double-underscore to back the property
        self.__init_lock = __import__("threading").Lock()

    @property
    def _collection(self):
        """Lazy-init: ChromaDB client + collection created on first access."""
        if self.__collection is None:
            with self.__init_lock:
                if self.__collection is None:
                    self._init_client()
        return self.__collection

    def _init_client(self) -> None:
        persist_dir = settings.CHROMA_PERSIST_DIR
        persist_dir.mkdir(parents=True, exist_ok=True)

        self._client = chromadb.PersistentClient(
            path=str(persist_dir),
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self.__collection = self._client.get_or_create_collection(
            name=self.collection_name,
            metadata={"description": "OmniCore scoped memory store"},
            embedding_function=_get_embedding_fn(),
        )
        log_agent_action(self.name, "Initialize", f"collection: {self.collection_name}")

    def _normalize_scope(self, scope: Optional[Dict[str, Any]]) -> Dict[str, str]:
        normalized: Dict[str, str] = {}
        if not isinstance(scope, dict):
            return normalized
        for key in _SCOPE_KEYS:
            value = sanitize_text(scope.get(key) or "")
            if value:
                normalized[key] = value
        return normalized

    def _scope_level(self, scope: Optional[Dict[str, Any]]) -> str:
        normalized = self._normalize_scope(scope)
        if normalized.get("todo_id"):
            return "todo"
        if normalized.get("project_id"):
            return "project"
        if normalized.get("goal_id"):
            return "goal"
        if normalized.get("session_id"):
            return "session"
        return "global"

    def _scope_key(self, scope: Optional[Dict[str, Any]]) -> str:
        normalized = self._normalize_scope(scope)
        if not normalized:
            return "global"
        return "|".join(f"{key}:{value}" for key, value in normalized.items())

    def _scope_metadata(self, scope: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized = self._normalize_scope(scope)
        meta: Dict[str, Any] = {"scope_level": self._scope_level(normalized), "scope_key": self._scope_key(normalized)}
        for key, value in normalized.items():
            meta[f"scope_{key}"] = value
        return meta

    def _scope_candidates(self, scope: Optional[Dict[str, Any]]) -> List[tuple[str, Dict[str, str]]]:
        """Build fallback chain: exact → project → goal → session → global.

        Progressively removes the most specific key so that a todo-level
        query falls back through project → goal → session → global.
        """
        normalized = self._normalize_scope(scope)
        if not normalized:
            return [("global", {})]

        candidates: List[tuple[str, Dict[str, str]]] = [("scope", normalized)]
        seen_keys: set[str] = {self._scope_key(normalized)}

        # Keys ordered from most specific to least specific
        current = dict(normalized)
        for drop_key in ("todo_id", "project_id", "goal_id"):
            if drop_key not in current:
                continue
            current = {k: v for k, v in current.items() if k != drop_key}
            if not current:
                break
            cache_key = self._scope_key(current)
            if cache_key not in seen_keys:
                level = self._scope_level(current)
                candidates.append((level, dict(current)))
                seen_keys.add(cache_key)

        candidates.append(("global", {}))
        return candidates

    def _build_where_filter(
        self,
        *,
        memory_type: Optional[str] = None,
        scope: Optional[Dict[str, Any]] = None,
        scope_level: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        clauses: List[Dict[str, Any]] = []
        if memory_type:
            clauses.append({"type": sanitize_text(memory_type)})
        normalized_scope = self._normalize_scope(scope)
        for key, value in normalized_scope.items():
            clauses.append({f"scope_{key}": value})
        if scope_level:
            clauses.append({"scope_level": sanitize_text(scope_level)})
        if not clauses:
            return None
        if len(clauses) == 1:
            return clauses[0]
        return {"$and": clauses}

    def _memory_id_from_fingerprint(self, fingerprint: str) -> str:
        digest = hashlib.sha1(sanitize_text(fingerprint).encode("utf-8")).hexdigest()[:24]
        return f"mem_{digest}"

    def _get_single_record(self, memory_id: str) -> Dict[str, Any]:
        try:
            result = self._collection.get(ids=[memory_id])
        except Exception:
            return {}
        ids = result.get("ids") or []
        if not ids:
            return {}
        return {
            "id": ids[0],
            "content": sanitize_text((result.get("documents") or [""])[0] or ""),
            "metadata": sanitize_value((result.get("metadatas") or [{}])[0] or {}),
        }

    def _query_collection(
        self,
        *,
        query: str,
        where_filter: Optional[Dict[str, Any]],
        n_results: int,
        scope_match: str,
    ) -> List[Dict[str, Any]]:
        clean_query = sanitize_text(query or "")
        if not clean_query or n_results <= 0:
            return []
        results = self._collection.query(
            query_texts=[clean_query],
            n_results=n_results,
            where=where_filter,
        )
        items: List[Dict[str, Any]] = []
        documents = (results.get("documents") or [[]])[0]
        ids = (results.get("ids") or [[]])[0]
        metadatas = (results.get("metadatas") or [[]])[0]
        distances = (results.get("distances") or [[]])[0]
        for index, document in enumerate(documents):
            metadata = sanitize_value(metadatas[index] if index < len(metadatas) else {})
            items.append(
                {
                    "id": ids[index] if index < len(ids) else "",
                    "content": sanitize_text(document or ""),
                    "metadata": metadata,
                    "distance": distances[index] if index < len(distances) else None,
                    "scope_match": scope_match,
                }
            )
        return items

    def _touch_memories(self, memories: List[Dict[str, Any]]) -> None:
        """Batch-update hit_count and last_accessed_at for retrieved memories."""
        if not memories:
            return
        timestamp = datetime.now().isoformat(timespec="seconds")
        ids: List[str] = []
        documents: List[str] = []
        metadatas: List[Dict[str, Any]] = []
        for memory in memories:
            memory_id = sanitize_text(memory.get("id") or "")
            if not memory_id:
                continue
            metadata = sanitize_value(memory.get("metadata") or {})
            metadata["last_accessed_at"] = timestamp
            metadata["hit_count"] = int(metadata.get("hit_count", 0) or 0) + 1
            ids.append(memory_id)
            documents.append(sanitize_text(memory.get("content") or ""))
            metadatas.append(metadata)
        if not ids:
            return
        try:
            self._collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
        except Exception as e:
            logger.debug("Failed to batch-touch %d memories: %s", len(ids), e)

    # Semantic dedup: below this distance threshold two memories are considered
    # duplicates.  ChromaDB distances are L2-squared by default; 0.15 is a
    # conservative threshold (very high similarity).
    DEDUP_DISTANCE_THRESHOLD = 0.15

    def add_memory(
        self,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        memory_type: str = "general",
        *,
        scope: Optional[Dict[str, Any]] = None,
        fingerprint: str = "",
        allow_update: bool = False,
        skip_dedup: bool = False,
    ) -> str:
        clean_content = sanitize_text(content or "")
        if not clean_content:
            return ""

        now = datetime.now().isoformat(timespec="seconds")
        existing: Dict[str, Any] = {}
        memory_id = f"mem_{uuid.uuid4().hex[:12]}"
        clean_fingerprint = sanitize_text(fingerprint or "")

        # --- semantic dedup for memories without fingerprint ---
        if not clean_fingerprint and not skip_dedup:
            try:
                where = self._build_where_filter(memory_type=memory_type, scope=scope)
                dupes = self._collection.query(
                    query_texts=[clean_content], n_results=1, where=where,
                )
                distances = (dupes.get("distances") or [[]])[0]
                dupe_ids = (dupes.get("ids") or [[]])[0]
                if distances and distances[0] < self.DEDUP_DISTANCE_THRESHOLD and dupe_ids:
                    logger.debug(
                        "Semantic dedup: skipping near-duplicate (distance=%.4f, existing=%s)",
                        distances[0], dupe_ids[0],
                    )
                    return dupe_ids[0]
            except Exception:
                pass  # dedup is best-effort
        if clean_fingerprint:
            memory_id = self._memory_id_from_fingerprint(clean_fingerprint)
            existing = self._get_single_record(memory_id)

        existing_metadata = sanitize_value(existing.get("metadata") or {})
        payload_metadata: Dict[str, Any] = {
            "type": sanitize_text(memory_type or "general"),
            "created_at": sanitize_text(existing_metadata.get("created_at") or now),
            "updated_at": now,
            "content_length": len(clean_content),
            "fingerprint": clean_fingerprint,
            "scope_level": self._scope_level(scope),
            "scope_key": self._scope_key(scope),
            "hit_count": int(existing_metadata.get("hit_count", 0) or 0),
            "revision": int(existing_metadata.get("revision", 0) or 0) + 1,
            "last_accessed_at": sanitize_text(existing_metadata.get("last_accessed_at") or ""),
        }
        payload_metadata.update(self._scope_metadata(scope))
        if metadata:
            payload_metadata.update(sanitize_value(metadata))

        # ChromaDB metadata 只接受标量，将 list/tuple 转为逗号拼接字符串
        for k, v in list(payload_metadata.items()):
            if isinstance(v, (list, tuple)):
                payload_metadata[k] = ",".join(str(x) for x in v)

        try:
            if clean_fingerprint and allow_update:
                self._collection.upsert(
                    ids=[memory_id],
                    documents=[clean_content],
                    metadatas=[payload_metadata],
                )
            else:
                self._collection.add(
                    ids=[memory_id],
                    documents=[clean_content],
                    metadatas=[payload_metadata],
                )
            log_agent_action(self.name, "Add memory", f"id: {memory_id}, type: {memory_type}")
            return memory_id
        except Exception as e:
            logger.error(f"Failed to add memory: {e}")
            return ""

    def search_memory(
        self,
        query: str,
        n_results: int = 5,
        memory_type: Optional[str] = None,
        *,
        scope: Optional[Dict[str, Any]] = None,
        include_global_fallback: bool = False,
        include_legacy_unscoped: bool = False,
    ) -> List[Dict[str, Any]]:
        clean_query = sanitize_text(query or "")
        if not clean_query:
            return []
        log_agent_action(self.name, "Search memory", clean_query[:60])

        search_specs: List[tuple[str, Optional[Dict[str, Any]]]] = []
        for scope_match, scope_candidate in self._scope_candidates(scope):
            if scope_match == "global":
                if not include_global_fallback and self._normalize_scope(scope):
                    continue
                search_specs.append(
                    (
                        scope_match,
                        self._build_where_filter(memory_type=memory_type, scope_level="global"),
                    )
                )
                continue
            search_specs.append(
                (
                    scope_match,
                    self._build_where_filter(memory_type=memory_type, scope=scope_candidate),
                )
            )

        memories: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        for scope_match, where_filter in search_specs:
            for item in self._query_collection(
                query=clean_query,
                where_filter=where_filter,
                n_results=n_results * 2,
                scope_match=scope_match,
            ):
                if item["id"] in seen_ids:
                    continue
                seen_ids.add(item["id"])
                memories.append(item)
                if len(memories) >= n_results:
                    self._touch_memories(memories[:n_results])
                    return memories[:n_results]

        if not memories and include_legacy_unscoped:
            legacy_items = self._query_collection(
                query=clean_query,
                where_filter=self._build_where_filter(memory_type=memory_type),
                n_results=n_results,
                scope_match="legacy_unscoped",
            )
            for item in legacy_items:
                if item["id"] in seen_ids:
                    continue
                seen_ids.add(item["id"])
                memories.append(item)
                if len(memories) >= n_results:
                    break

        self._touch_memories(memories[:n_results])
        return memories[:n_results]

    def _records_from_get(
        self,
        *,
        where_filter: Optional[Dict[str, Any]],
        scope_match: str,
    ) -> List[Dict[str, Any]]:
        results = self._collection.get(where=where_filter)
        documents = results.get("documents") or []
        ids = results.get("ids") or []
        metadatas = results.get("metadatas") or []
        items: List[Dict[str, Any]] = []
        for index, document in enumerate(documents):
            items.append(
                {
                    "id": ids[index] if index < len(ids) else "",
                    "content": sanitize_text(document or ""),
                    "metadata": sanitize_value(metadatas[index] if index < len(metadatas) else {}),
                    "scope_match": scope_match,
                }
            )
        return items

    def get_recent_memories(
        self,
        limit: int = 10,
        memory_type: Optional[str] = None,
        *,
        scope: Optional[Dict[str, Any]] = None,
        include_global_fallback: bool = False,
    ) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        normalized_scope = self._normalize_scope(scope)
        if normalized_scope:
            records.extend(
                self._records_from_get(
                    where_filter=self._build_where_filter(memory_type=memory_type, scope=normalized_scope),
                    scope_match="scope",
                )
            )
            if include_global_fallback:
                records.extend(
                    self._records_from_get(
                        where_filter=self._build_where_filter(memory_type=memory_type, scope_level="global"),
                        scope_match="global",
                    )
                )
        else:
            records.extend(
                self._records_from_get(
                    where_filter=self._build_where_filter(memory_type=memory_type),
                    scope_match="global",
                )
            )

        records.sort(
            key=lambda item: (
                str((item.get("metadata") or {}).get("updated_at") or ""),
                str((item.get("metadata") or {}).get("created_at") or ""),
            ),
            reverse=True,
        )
        return records[: max(limit, 0)]

    def delete_memory(self, memory_id: str) -> bool:
        try:
            self._collection.delete(ids=[sanitize_text(memory_id or "")])
            log_agent_action(self.name, "Delete memory", sanitize_text(memory_id or ""))
            return True
        except Exception as e:
            logger.error(f"Failed to delete memory: {e}")
            return False

    def clear_scope(
        self,
        scope: Optional[Dict[str, Any]],
        *,
        memory_type: Optional[str] = None,
    ) -> int:
        where_filter = self._build_where_filter(memory_type=memory_type, scope=scope)
        if not where_filter:
            return 0
        results = self._collection.get(where=where_filter)
        ids = [sanitize_text(item or "") for item in results.get("ids") or [] if sanitize_text(item or "")]
        if not ids:
            return 0
        try:
            self._collection.delete(ids=ids)
            log_agent_action(self.name, "Clear scope", f"deleted: {len(ids)}")
            return len(ids)
        except Exception as e:
            logger.error(f"Failed to clear scoped memory: {e}")
            return 0

    def clear_all(self) -> bool:
        try:
            self._client.delete_collection(self.collection_name)
            self.__collection = self._client.get_or_create_collection(
                name=self.collection_name,
                embedding_function=_get_embedding_fn(),
            )
            log_success("Memory store cleared")
            return True
        except Exception as e:
            logger.error(f"Failed to clear memory: {e}")
            return False

    def save_task_result(
        self,
        task_description: str,
        result: Any,
        success: bool,
        *,
        scope: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        fingerprint: str = "",
    ) -> str:
        clean_description = sanitize_text(task_description or "")
        rendered_result = sanitize_text(str(result)[:1500])
        content = f"Task: {clean_description}\nOutcome: {rendered_result}"
        extra_metadata = {
            "task_description": clean_description,
            "success": bool(success),
        }
        if metadata:
            extra_metadata.update(sanitize_value(metadata))
        resolved_fingerprint = sanitize_text(fingerprint or "")
        if not resolved_fingerprint:
            payload = {
                "scope": self._scope_key(scope),
                "task": clean_description,
                "success": bool(success),
                "outcome": rendered_result[:400],
            }
            resolved_fingerprint = hashlib.sha1(
                json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:24]
        return self.add_memory(
            content=content,
            metadata=extra_metadata,
            memory_type="task_result",
            scope=scope,
            fingerprint=resolved_fingerprint,
            allow_update=True,
        )

    def save_user_preference(
        self,
        preference_key: str,
        preference_value: str,
        *,
        scope: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        clean_key = sanitize_text(preference_key or "")
        clean_value = sanitize_text(preference_value or "")
        content = f"User preference - {clean_key}: {clean_value}"
        extra_metadata = {
            "preference_key": clean_key,
            "preference_value": clean_value,
        }
        if metadata:
            extra_metadata.update(sanitize_value(metadata))
        fingerprint = f"preference:{self._scope_key(scope)}:{clean_key}"
        return self.add_memory(
            content=content,
            metadata=extra_metadata,
            memory_type="preference",
            scope=scope,
            fingerprint=fingerprint,
            allow_update=True,
        )

    def evict_stale(
        self,
        *,
        max_age_days: int = 90,
        min_hit_count: int = 0,
        scope: Optional[Dict[str, Any]] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Evict memories that are older than *max_age_days* AND have a hit_count
        at or below *min_hit_count*.  Preferences are never evicted automatically.

        Returns ``{"evicted": int, "scanned": int, "candidates": [ids]}``
        """
        where_filter = self._build_where_filter(scope=scope) if scope else None
        results = self._collection.get(where=where_filter)
        ids = results.get("ids") or []
        metadatas = results.get("metadatas") or []
        cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat(timespec="seconds")

        candidates: List[str] = []
        for idx, memory_id in enumerate(ids):
            meta = sanitize_value(metadatas[idx] if idx < len(metadatas) else {})
            # Never auto-evict preferences
            if sanitize_text(meta.get("type") or "") == "preference":
                continue
            updated = sanitize_text(meta.get("updated_at") or meta.get("created_at") or "")
            if not updated or updated >= cutoff:
                continue
            hit = int(meta.get("hit_count", 0) or 0)
            if hit <= min_hit_count:
                candidates.append(sanitize_text(memory_id))

        evicted = 0
        if candidates and not dry_run:
            try:
                self._collection.delete(ids=candidates)
                evicted = len(candidates)
                log_agent_action(self.name, "Evict stale", f"evicted {evicted}/{len(ids)}")
            except Exception as e:
                logger.error("Failed to evict stale memories: %s", e)

        return {
            "scanned": len(ids),
            "evicted": evicted if not dry_run else 0,
            "candidates": len(candidates),
            "dry_run": dry_run,
        }

    def get_stats(self, *, scope: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        where_filter = self._build_where_filter(scope=scope) if scope else None
        results = self._collection.get(where=where_filter)
        metadatas = results.get("metadatas") or []
        by_type: Dict[str, int] = {}
        by_scope_level: Dict[str, int] = {}
        for metadata in metadatas:
            meta = sanitize_value(metadata or {})
            memory_type = sanitize_text(meta.get("type") or "unknown")
            scope_level = sanitize_text(meta.get("scope_level") or "legacy")
            by_type[memory_type] = by_type.get(memory_type, 0) + 1
            by_scope_level[scope_level] = by_scope_level.get(scope_level, 0) + 1
        return {
            "collection_name": self.collection_name,
            "total_memories": len(results.get("ids") or []),
            "persist_dir": str(settings.CHROMA_PERSIST_DIR),
            "scope": self._normalize_scope(scope),
            "by_type": by_type,
            "by_scope_level": by_scope_level,
        }
