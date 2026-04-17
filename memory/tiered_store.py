"""
Tiered memory store (A4).

Splits memories across three Chroma collections so each tier can have
its own lifecycle, weight, and eviction policy:

- ``working``:  session-scoped scratch. Purged when session closes.
- ``episodic``: task outcomes, artifact pointers. Standard retention.
- ``semantic``: stable facts, consolidated summaries, learned preferences.
                Weighted up during retrieval.

The tiered store is off by default (``MEMORY_TIERED_ENABLED=false``);
when disabled, the single collection ``omnicore_memory`` path stays
canonical and this module is inert.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from config.settings import settings
from utils.logger import log_agent_action, log_warning

if TYPE_CHECKING:
    from memory.scoped_chroma_store import ChromaMemory


class MemoryTier(str, Enum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"


# Default tier assignment per memory_type. Callers can override by passing
# an explicit tier to ``add``.
_TYPE_DEFAULT_TIER: Dict[str, MemoryTier] = {
    "preference": MemoryTier.SEMANTIC,
    "consolidated_summary": MemoryTier.SEMANTIC,
    "skill_definition": MemoryTier.SEMANTIC,
    "entity_record": MemoryTier.SEMANTIC,
    "task_result": MemoryTier.EPISODIC,
    "artifact_reference": MemoryTier.EPISODIC,
    "general": MemoryTier.EPISODIC,
}


def default_tier_for_type(memory_type: str) -> MemoryTier:
    return _TYPE_DEFAULT_TIER.get((memory_type or "").strip(), MemoryTier.EPISODIC)


def tier_weight(tier: MemoryTier) -> float:
    if tier == MemoryTier.WORKING:
        return float(settings.MEMORY_TIER_WEIGHT_WORKING)
    if tier == MemoryTier.EPISODIC:
        return float(settings.MEMORY_TIER_WEIGHT_EPISODIC)
    return float(settings.MEMORY_TIER_WEIGHT_SEMANTIC)


class TieredMemoryStore:
    """Thin façade over three ChromaMemory collections.

    The class holds references lazily; collections are created only on
    first access, same pattern as ``ChromaMemory``.
    """

    def __init__(
        self,
        *,
        working: Optional["ChromaMemory"] = None,
        episodic: Optional["ChromaMemory"] = None,
        semantic: Optional["ChromaMemory"] = None,
        legacy: Optional["ChromaMemory"] = None,
    ) -> None:
        self._stores: Dict[MemoryTier, Optional["ChromaMemory"]] = {
            MemoryTier.WORKING: working,
            MemoryTier.EPISODIC: episodic,
            MemoryTier.SEMANTIC: semantic,
        }
        self._legacy = legacy

    # ------------------------------------------------------------------
    # lazy accessors
    # ------------------------------------------------------------------

    def _get_store(self, tier: MemoryTier) -> Optional["ChromaMemory"]:
        existing = self._stores.get(tier)
        if existing is not None:
            return existing
        if not settings.MEMORY_TIERED_ENABLED:
            return None
        try:
            from memory.scoped_chroma_store import ChromaMemory
            collection = {
                MemoryTier.WORKING: settings.MEMORY_TIER_WORKING_COLLECTION,
                MemoryTier.EPISODIC: settings.MEMORY_TIER_EPISODIC_COLLECTION,
                MemoryTier.SEMANTIC: settings.MEMORY_TIER_SEMANTIC_COLLECTION,
            }[tier]
            store = ChromaMemory(collection_name=collection, silent=True)
            self._stores[tier] = store
            return store
        except Exception as exc:
            log_warning(f"TieredMemoryStore tier={tier.value} init failed: {exc}")
            return None

    def _get_legacy(self) -> Optional["ChromaMemory"]:
        """Legacy ``omnicore_memory`` collection, used as read-time fallback
        so existing data keeps being retrievable after the tiered switch is
        flipped on but before the one-shot migration has run.
        """
        if self._legacy is not None:
            return self._legacy
        if not settings.MEMORY_TIER_LEGACY_FALLBACK:
            return None
        try:
            from memory.scoped_chroma_store import ChromaMemory
            self._legacy = ChromaMemory(
                collection_name=settings.MEMORY_TIER_LEGACY_COLLECTION,
                silent=True,
            )
            return self._legacy
        except Exception as exc:
            log_warning(f"TieredMemoryStore legacy fallback init failed: {exc}")
            return None

    def store_for_type(
        self,
        memory_type: str,
        *,
        tier: Optional[MemoryTier] = None,
    ) -> Optional["ChromaMemory"]:
        """Return the underlying ``ChromaMemory`` for a given memory type.

        Used by ``MemoryManager`` so the existing ``save_task_result`` /
        ``save_user_preference`` helpers stay as-is while writes land in
        the correct tier collection.
        """
        effective = tier or default_tier_for_type(memory_type)
        return self._get_store(effective)

    # ------------------------------------------------------------------
    # write
    # ------------------------------------------------------------------

    def add(
        self,
        content: str,
        *,
        memory_type: str = "general",
        tier: Optional[MemoryTier] = None,
        scope: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        fingerprint: str = "",
        allow_update: bool = False,
        skip_dedup: bool = False,
    ) -> str:
        effective_tier = tier or default_tier_for_type(memory_type)
        store = self._get_store(effective_tier)
        if store is None:
            return ""
        return store.add_memory(
            content=content,
            metadata=metadata,
            memory_type=memory_type,
            scope=scope,
            fingerprint=fingerprint,
            allow_update=allow_update,
            skip_dedup=skip_dedup,
        )

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        tiers: Optional[List[MemoryTier]] = None,
        memory_type: Optional[str] = None,
        scope: Optional[Dict[str, Any]] = None,
        n_results: int = 5,
        include_global_fallback: bool = True,
    ) -> List[Dict[str, Any]]:
        active_tiers = tiers or list(MemoryTier)
        merged: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        for tier in active_tiers:
            store = self._get_store(tier)
            if store is None:
                continue
            try:
                items = store.search_memory(
                    query,
                    n_results=n_results,
                    memory_type=memory_type,
                    scope=scope,
                    include_global_fallback=include_global_fallback,
                )
            except Exception as exc:
                log_warning(f"TieredMemoryStore search tier={tier.value} failed: {exc}")
                continue
            weight = tier_weight(tier)
            for item in items:
                if item.get("id") in seen_ids:
                    continue
                seen_ids.add(item["id"])
                score_source = item.get("decay_score")
                if score_source is None:
                    # fall back to 1 - distance
                    dist = item.get("distance")
                    score_source = max(0.0, 1.0 - float(dist if dist is not None else 1.0))
                item_copy = dict(item)
                item_copy["tier"] = tier.value
                item_copy["tier_weight"] = weight
                item_copy["tier_score"] = float(score_source) * weight
                merged.append(item_copy)
        merged.sort(key=lambda x: x.get("tier_score", 0.0), reverse=True)

        # Legacy fallback: when no tier returned anything, consult the
        # untouched ``omnicore_memory`` collection so pre-migration data
        # remains visible.
        if not merged:
            legacy = self._get_legacy()
            if legacy is not None:
                try:
                    items = legacy.search_memory(
                        query,
                        n_results=n_results,
                        memory_type=memory_type,
                        scope=scope,
                        include_global_fallback=include_global_fallback,
                        include_legacy_unscoped=True,
                    )
                except Exception as exc:
                    log_warning(f"TieredMemoryStore legacy search failed: {exc}")
                    items = []
                for item in items:
                    dist = item.get("distance")
                    score_source = max(0.0, 1.0 - float(dist if dist is not None else 1.0))
                    item_copy = dict(item)
                    item_copy["tier"] = "legacy"
                    item_copy["tier_weight"] = 1.0
                    item_copy["tier_score"] = float(score_source)
                    merged.append(item_copy)
                merged.sort(key=lambda x: x.get("tier_score", 0.0), reverse=True)

        return merged[: max(n_results, 0)]

    # ------------------------------------------------------------------
    # housekeeping
    # ------------------------------------------------------------------

    def purge_working(self, session_id: str) -> int:
        """Delete all working-tier memories scoped to the given session."""
        if not settings.MEMORY_TIERED_ENABLED or not session_id:
            return 0
        store = self._get_store(MemoryTier.WORKING)
        if store is None:
            return 0
        try:
            deleted = store.clear_scope({"session_id": session_id})
            log_agent_action("TieredMemoryStore", "Purged working", f"session={session_id} n={deleted}")
            return int(deleted or 0)
        except Exception as exc:
            log_warning(f"Purge working tier failed: {exc}")
            return 0

    def stats(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for tier in MemoryTier:
            store = self._get_store(tier)
            if store is None:
                out[tier.value] = {"available": False}
                continue
            try:
                out[tier.value] = store.get_stats()
            except Exception as exc:
                out[tier.value] = {"error": str(exc)}
        return out
