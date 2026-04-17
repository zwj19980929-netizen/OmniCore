"""
Entity inverted index (A2).

A separate Chroma collection that tracks every distinct entity extracted
from task outcomes, alongside the memory IDs where it appeared and how
often. Enables faceted queries like "all tasks mentioning Acme" and
"top entities the user has touched recently".

Design notes:
- Each entity is one record, fingerprinted by (type, text), so re-inserts
  upsert and accumulate ``occurrence_count`` rather than creating dupes.
- The document body is ``"{type}: {text}"`` so semantic search still works
  when users ask with slightly different phrasing.
- All operations are best-effort; failures must not break the write path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from config.settings import settings
from utils.logger import log_agent_action, log_warning
from utils.text import sanitize_text, sanitize_value

if TYPE_CHECKING:
    from memory.scoped_chroma_store import ChromaMemory


@dataclass
class EntityRecord:
    entity_type: str
    entity_text: str
    occurrence_count: int = 0
    first_seen: str = ""
    last_seen: str = ""
    related_memory_ids: List[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        return f"entity:{self.entity_type.lower()}:{self.entity_text.lower()}"


class EntityIndex:
    """Lightweight entity store built on top of ChromaMemory."""

    MEMORY_TYPE = "entity_record"

    def __init__(self, store: Optional["ChromaMemory"] = None) -> None:
        self._store = store

    @property
    def store(self) -> Optional["ChromaMemory"]:
        if self._store is None and settings.MEMORY_ENTITY_INDEX_ENABLED:
            try:
                from memory.scoped_chroma_store import ChromaMemory
                self._store = ChromaMemory(
                    collection_name=settings.MEMORY_ENTITY_INDEX_COLLECTION,
                    silent=True,
                )
            except Exception as exc:
                log_warning(f"EntityIndex init failed: {exc}")
                self._store = None
        return self._store

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def record(
        self,
        entity_type: str,
        entity_text: str,
        *,
        memory_id: str = "",
        scope: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Upsert an entity record. Returns the chroma memory_id or ``""``.
        """
        if not settings.MEMORY_ENTITY_INDEX_ENABLED:
            return ""
        store = self.store
        if store is None:
            return ""
        etype = sanitize_text(str(entity_type or "KEYWORD")).upper()
        etext = sanitize_text(str(entity_text or ""))
        if not etext:
            return ""

        rec = EntityRecord(entity_type=etype, entity_text=etext)
        fingerprint = rec.fingerprint
        # Reuse ChromaMemory upsert semantics: fetch existing to accumulate
        existing = store._get_single_record(store._memory_id_from_fingerprint(fingerprint))
        now = datetime.now().isoformat(timespec="seconds")
        prev_meta = existing.get("metadata") or {}
        occurrence = int(prev_meta.get("occurrence_count", 0) or 0) + 1
        first_seen = sanitize_text(str(prev_meta.get("first_seen") or now))
        prev_ids = sanitize_text(str(prev_meta.get("related_memory_ids") or ""))
        ids_list = [x for x in prev_ids.split(",") if x]
        if memory_id and memory_id not in ids_list:
            ids_list.append(memory_id)
            ids_list = ids_list[-50:]  # cap to most recent 50

        content = f"{etype}: {etext}"
        metadata = {
            "entity_type": etype,
            "entity_text": etext,
            "occurrence_count": occurrence,
            "first_seen": first_seen,
            "last_seen": now,
            "related_memory_ids": ",".join(ids_list),
        }
        return store.add_memory(
            content=content,
            metadata=metadata,
            memory_type=self.MEMORY_TYPE,
            scope=scope,
            fingerprint=fingerprint,
            allow_update=True,
            skip_dedup=True,
        )

    def record_many(
        self,
        entities: List[Dict[str, Any]],
        *,
        memory_id: str = "",
        scope: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Record a list of ``{type, text}`` dicts. Returns count written."""
        if not entities:
            return 0
        count = 0
        for ent in entities[:20]:
            try:
                written = self.record(
                    entity_type=str(ent.get("type") or "KEYWORD"),
                    entity_text=str(ent.get("text") or ""),
                    memory_id=memory_id,
                    scope=scope,
                )
                if written:
                    count += 1
            except Exception as exc:
                log_warning(f"EntityIndex record failed: {exc}")
        if count:
            log_agent_action("EntityIndex", "Recorded", f"{count} entities")
        return count

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def search(
        self,
        entity_text: str,
        *,
        entity_type: Optional[str] = None,
        limit: int = 10,
    ) -> List[EntityRecord]:
        """Semantic + fuzzy match over entity records."""
        if not settings.MEMORY_ENTITY_INDEX_ENABLED:
            return []
        store = self.store
        if store is None or not entity_text:
            return []
        query = entity_text
        if entity_type:
            query = f"{entity_type}: {entity_text}"
        items = store.search_memory(
            query,
            n_results=max(limit * 2, limit),
            memory_type=self.MEMORY_TYPE,
            include_global_fallback=True,
            include_legacy_unscoped=True,
        )
        records: List[EntityRecord] = []
        want_type = (entity_type or "").upper().strip()
        for item in items:
            rec = self._record_from_item(item)
            if rec is None:
                continue
            if want_type and rec.entity_type != want_type:
                continue
            records.append(rec)
            if len(records) >= limit:
                break
        return records

    def delete_by_entity(
        self,
        entity_text: str,
        *,
        entity_type: Optional[str] = None,
    ) -> int:
        """Delete all records matching the given entity text / type.

        Used for targeted cleanup (e.g. after the user deletes a project
        whose entity should no longer surface in top-K). Returns the count
        of deleted rows; silently returns 0 when disabled or missing.
        """
        if not settings.MEMORY_ENTITY_INDEX_ENABLED:
            return 0
        store = self.store
        if store is None:
            return 0
        etype = (entity_type or "").upper().strip()
        etext = sanitize_text(str(entity_text or ""))
        if not etext:
            return 0
        try:
            raw = store._collection.get()
        except Exception as exc:
            log_warning(f"EntityIndex delete fetch failed: {exc}")
            return 0
        ids = raw.get("ids") or []
        metadatas = raw.get("metadatas") or []
        victims: List[str] = []
        for idx, memory_id in enumerate(ids):
            meta = metadatas[idx] if idx < len(metadatas) else {}
            if not isinstance(meta, dict):
                continue
            row_type = sanitize_text(str(meta.get("entity_type") or "")).upper()
            row_text = sanitize_text(str(meta.get("entity_text") or ""))
            if etype and row_type != etype:
                continue
            if row_text.lower() != etext.lower():
                continue
            victims.append(memory_id)
        if not victims:
            return 0
        try:
            store._collection.delete(ids=victims)
        except Exception as exc:
            log_warning(f"EntityIndex delete failed: {exc}")
            return 0
        log_agent_action("EntityIndex", "Deleted", f"{len(victims)} records for {entity_type or '*'}:{entity_text}")
        return len(victims)

    def top_entities(
        self,
        *,
        entity_type: Optional[str] = None,
        limit: int = 10,
    ) -> List[EntityRecord]:
        """Return most-frequently seen entities, optionally filtered by type."""
        if not settings.MEMORY_ENTITY_INDEX_ENABLED:
            return []
        store = self.store
        if store is None:
            return []
        try:
            raw = store._collection.get()
        except Exception as exc:
            log_warning(f"EntityIndex top fetch failed: {exc}")
            return []
        ids = raw.get("ids") or []
        documents = raw.get("documents") or []
        metadatas = raw.get("metadatas") or []
        want_type = (entity_type or "").upper().strip()

        records: List[EntityRecord] = []
        for idx, memory_id in enumerate(ids):
            meta = sanitize_value(metadatas[idx] if idx < len(metadatas) else {})
            rec = self._record_from_item(
                {
                    "id": memory_id,
                    "content": documents[idx] if idx < len(documents) else "",
                    "metadata": meta,
                }
            )
            if rec is None:
                continue
            if want_type and rec.entity_type != want_type:
                continue
            records.append(rec)
        records.sort(key=lambda r: r.occurrence_count, reverse=True)
        return records[:max(limit, 0)]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _record_from_item(item: Dict[str, Any]) -> Optional[EntityRecord]:
        meta = item.get("metadata") or {}
        etype = sanitize_text(str(meta.get("entity_type") or "")).upper()
        etext = sanitize_text(str(meta.get("entity_text") or ""))
        if not etype or not etext:
            return None
        ids_raw = sanitize_text(str(meta.get("related_memory_ids") or ""))
        ids_list = [x for x in ids_raw.split(",") if x]
        return EntityRecord(
            entity_type=etype,
            entity_text=etext,
            occurrence_count=int(meta.get("occurrence_count", 0) or 0),
            first_seen=sanitize_text(str(meta.get("first_seen") or "")),
            last_seen=sanitize_text(str(meta.get("last_seen") or "")),
            related_memory_ids=ids_list,
        )
