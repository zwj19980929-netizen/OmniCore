"""
Memory consolidation (A1).

Scans the Chroma store for memories past TTL and either:
1. Deletes outright those with hit_count < MEMORY_CONSOLIDATION_MIN_HITS
   (never touched — no proven value)
2. Groups still-useful stale memories by scope and sends each group to an
   LLM for a "phase summary"; writes the summary back as a single
   ``consolidated_summary`` memory and deletes the originals.

Safe defaults:
- When ``MEMORY_CONSOLIDATION_ENABLED=false`` the LLM path is skipped; only
  the straight-delete branch runs.
- ``dry_run=True`` reports candidates without writing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from config.settings import settings
from utils.logger import log_agent_action, log_warning, logger
from utils.text import sanitize_text, sanitize_value

if TYPE_CHECKING:
    from memory.scoped_chroma_store import ChromaMemory


@dataclass
class ConsolidationReport:
    scanned: int = 0
    deleted_never_used: int = 0
    consolidated_groups: int = 0
    consolidated_memories: int = 0
    skipped_diverse: int = 0
    errors: int = 0
    details: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "scanned": self.scanned,
            "deleted_never_used": self.deleted_never_used,
            "consolidated_groups": self.consolidated_groups,
            "consolidated_memories": self.consolidated_memories,
            "skipped_diverse": self.skipped_diverse,
            "errors": self.errors,
            "details": self.details,
        }


_PROMPT_PATH = Path(settings.PROJECT_ROOT) / "prompts" / "memory_consolidation.txt"


def _load_prompt() -> str:
    try:
        return _PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Consolidation prompt not found: %s", _PROMPT_PATH)
        return ""


def _cutoff_iso(ttl_days: int, now: Optional[datetime] = None) -> str:
    reference = now or datetime.now()
    return (reference - timedelta(days=max(int(ttl_days), 1))).isoformat(timespec="seconds")


def _scope_from_metadata(metadata: Dict[str, Any]) -> Dict[str, str]:
    """Reconstruct the scope dict from stored metadata fields."""
    scope: Dict[str, str] = {}
    for key in ("session_id", "goal_id", "project_id", "todo_id"):
        value = sanitize_text(str(metadata.get(f"scope_{key}", "") or ""))
        if value:
            scope[key] = value
    return scope


def _parse_llm_json(content: str) -> Optional[Dict[str, Any]]:
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        lines = [l for l in text.split("\n") if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except (json.JSONDecodeError, TypeError):
                return None
    return None


def _format_batch_for_llm(records: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for idx, rec in enumerate(records, 1):
        meta = rec.get("metadata") or {}
        lines.append(
            f"### 记忆 {idx}\n"
            f"- 类型: {sanitize_text(str(meta.get('type', '') or 'general'))}\n"
            f"- 创建时间: {sanitize_text(str(meta.get('created_at', '') or ''))}\n"
            f"- 命中次数: {int(meta.get('hit_count', 0) or 0)}\n"
            f"- 内容: {sanitize_text(str(rec.get('content', '') or ''))[:800]}"
        )
    return "\n\n".join(lines)


def _list_stale_candidates(
    store: "ChromaMemory",
    cutoff: str,
) -> List[Dict[str, Any]]:
    """Pull all records from the collection, filter by stale cutoff in-python.

    ChromaDB's ``$lt`` operator on string metadata is supported but varies by
    version; filtering in-python is a small cost for a background job and
    avoids version-specific issues.
    """
    try:
        results = store._collection.get()
    except Exception as exc:
        log_warning(f"Consolidator: collection get failed: {exc}")
        return []
    ids = results.get("ids") or []
    documents = results.get("documents") or []
    metadatas = results.get("metadatas") or []
    candidates: List[Dict[str, Any]] = []
    for idx, memory_id in enumerate(ids):
        meta = sanitize_value(metadatas[idx] if idx < len(metadatas) else {})
        mem_type = sanitize_text(str(meta.get("type", "") or ""))
        # never auto-consolidate preferences or already-consolidated summaries
        if mem_type in {"preference", "consolidated_summary", "skill_definition"}:
            continue
        stamp = sanitize_text(str(meta.get("updated_at") or meta.get("created_at") or ""))
        if not stamp or stamp >= cutoff:
            continue
        candidates.append(
            {
                "id": sanitize_text(memory_id),
                "content": sanitize_text(str(documents[idx] if idx < len(documents) else "")),
                "metadata": meta,
            }
        )
    return candidates


def _group_by_scope(
    records: List[Dict[str, Any]],
    batch_size: int,
) -> List[List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        key = sanitize_text(str((rec.get("metadata") or {}).get("scope_key") or "global"))
        groups.setdefault(key, []).append(rec)
    batches: List[List[Dict[str, Any]]] = []
    for records_in_scope in groups.values():
        # split every scope into chunks ≤ batch_size
        records_in_scope.sort(
            key=lambda r: str((r.get("metadata") or {}).get("created_at") or "")
        )
        for start in range(0, len(records_in_scope), batch_size):
            batches.append(records_in_scope[start : start + batch_size])
    return batches


def consolidate_expired(
    store: "ChromaMemory",
    *,
    ttl_days: Optional[int] = None,
    min_hits: Optional[int] = None,
    batch_size: Optional[int] = None,
    model: Optional[str] = None,
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> ConsolidationReport:
    """Scan *store* for memories older than TTL and consolidate or delete them.

    Args:
        store: A ``ChromaMemory`` instance (or any object with the same API).
        ttl_days: Override for ``MEMORY_TTL_DAYS``.
        min_hits: Minimum hit_count to be eligible for LLM consolidation.
                  Below this → straight delete. Default from settings.
        batch_size: Max records per LLM call.
        model: LLM model override; empty string = client default.
        dry_run: Report candidates without writing.
    """
    report = ConsolidationReport()
    ttl = int(ttl_days if ttl_days is not None else settings.MEMORY_TTL_DAYS)
    cutoff = _cutoff_iso(ttl, now=now)
    min_hits_eff = int(
        min_hits if min_hits is not None else settings.MEMORY_CONSOLIDATION_MIN_HITS
    )
    bsize = int(batch_size or settings.MEMORY_CONSOLIDATION_BATCH_SIZE)
    llm_enabled = bool(settings.MEMORY_CONSOLIDATION_ENABLED)
    effective_model = str(model if model is not None else settings.MEMORY_CONSOLIDATION_MODEL or "")

    stale = _list_stale_candidates(store, cutoff)
    report.scanned = len(stale)
    if not stale:
        return report

    to_delete: List[str] = []
    to_consolidate: List[Dict[str, Any]] = []
    for rec in stale:
        hits = int((rec.get("metadata") or {}).get("hit_count", 0) or 0)
        if hits < min_hits_eff:
            to_delete.append(rec["id"])
        else:
            to_consolidate.append(rec)

    # --- Step 1: straight delete never-used stale records ---
    if to_delete:
        if not dry_run:
            try:
                store._collection.delete(ids=to_delete)
                log_agent_action(
                    "MemoryConsolidator",
                    "Deleted never-used",
                    f"{len(to_delete)}",
                )
            except Exception as exc:
                log_warning(f"Consolidator delete failed: {exc}")
                report.errors += 1
        report.deleted_never_used = len(to_delete)

    # --- Step 2: LLM consolidation (only when explicitly enabled) ---
    if not llm_enabled or not to_consolidate:
        return report

    prompt = _load_prompt()
    if not prompt:
        report.errors += 1
        return report

    from core.llm import LLMClient
    llm = LLMClient(model=effective_model) if effective_model else LLMClient()

    for batch in _group_by_scope(to_consolidate, bsize):
        payload = _format_batch_for_llm(batch)
        user_message = (
            "请对以下这批同一 scope 的老记忆进行归档摘要:\n\n" + payload
        )
        try:
            response = llm.chat_with_system(
                system_prompt=prompt,
                user_message=user_message,
                temperature=0.2,
                json_mode=True,
            )
        except Exception as exc:
            log_warning(f"Consolidator LLM call failed: {exc}")
            report.errors += 1
            continue

        parsed = _parse_llm_json(response.content if response else "")
        if not parsed:
            report.errors += 1
            continue

        summary = sanitize_text(str(parsed.get("summary") or ""))
        key_points = parsed.get("key_points") or []
        if (not summary) or "topics_too_diverse" in ",".join(str(k) for k in key_points):
            report.skipped_diverse += 1
            continue

        first_meta = batch[0].get("metadata") or {}
        scope = _scope_from_metadata(first_meta)
        time_range = sanitize_text(str(parsed.get("time_range") or ""))
        entities = ",".join(str(e) for e in (parsed.get("entities") or [])[:10])
        archived_ids = [rec["id"] for rec in batch]

        if not dry_run:
            try:
                store.add_memory(
                    content=summary,
                    metadata={
                        "time_range": time_range,
                        "entities": entities,
                        "archived_ids": ",".join(archived_ids),
                        "archived_count": str(len(archived_ids)),
                        "source": "consolidation",
                    },
                    memory_type="consolidated_summary",
                    scope=scope,
                    fingerprint=f"consolidated:{first_meta.get('scope_key', 'global')}:{archived_ids[0]}",
                    allow_update=True,
                    skip_dedup=True,
                )
                store._collection.delete(ids=archived_ids)
            except Exception as exc:
                log_warning(f"Consolidator write/delete failed: {exc}")
                report.errors += 1
                continue

        report.consolidated_groups += 1
        report.consolidated_memories += len(archived_ids)
        report.details.append(
            {
                "scope_key": sanitize_text(str(first_meta.get("scope_key") or "global")),
                "archived_count": len(archived_ids),
                "summary_preview": summary[:160],
            }
        )

    log_agent_action(
        "MemoryConsolidator",
        "Run complete",
        json.dumps(report.as_dict(), ensure_ascii=False)[:200],
    )
    return report
