#!/usr/bin/env python
"""
Migrate records from the legacy ``omnicore_memory`` collection into the
three tiered collections (A4).

Usage:
    python scripts/migrate_memory_tiers.py [--dry-run]

Strategy:
- Read every record from the source collection.
- Route each record by ``memory_type`` via ``memory.tiered_store.default_tier_for_type``.
- Upsert into the target collection (fingerprint kept stable to preserve identity).
- Never deletes from the source; the legacy collection stays as a read-only fallback.

Safe to re-run: upserts are idempotent.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# allow running as script: prepend project root to sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.settings import settings  # noqa: E402
from memory.scoped_chroma_store import ChromaMemory  # noqa: E402
from memory.tiered_store import MemoryTier, default_tier_for_type  # noqa: E402


def _tier_collection(tier: MemoryTier) -> str:
    return {
        MemoryTier.WORKING: settings.MEMORY_TIER_WORKING_COLLECTION,
        MemoryTier.EPISODIC: settings.MEMORY_TIER_EPISODIC_COLLECTION,
        MemoryTier.SEMANTIC: settings.MEMORY_TIER_SEMANTIC_COLLECTION,
    }[tier]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Report plan without writing")
    parser.add_argument(
        "--source",
        default="omnicore_memory",
        help="Source collection name (default: omnicore_memory)",
    )
    args = parser.parse_args()

    source = ChromaMemory(collection_name=args.source, silent=True)
    try:
        raw = source._collection.get()
    except Exception as exc:
        print(f"[error] failed to read source collection: {exc}")
        return 1

    ids = raw.get("ids") or []
    documents = raw.get("documents") or []
    metadatas = raw.get("metadatas") or []
    total = len(ids)
    print(f"Source '{args.source}' has {total} records.")

    counts = {tier: 0 for tier in MemoryTier}
    targets = {tier: ChromaMemory(collection_name=_tier_collection(tier), silent=True) for tier in MemoryTier}

    for idx, memory_id in enumerate(ids):
        meta = metadatas[idx] if idx < len(metadatas) else {}
        content = documents[idx] if idx < len(documents) else ""
        memory_type = str(meta.get("type") or "general")
        tier = default_tier_for_type(memory_type)
        counts[tier] += 1

        if args.dry_run:
            continue

        try:
            targets[tier]._collection.upsert(
                ids=[memory_id],
                documents=[content],
                metadatas=[dict(meta or {})],
            )
        except Exception as exc:
            print(f"[warn] upsert to {tier.value} failed for {memory_id}: {exc}")

    print("Migration plan (memory_type → tier count):")
    for tier, count in counts.items():
        print(f"  {tier.value}: {count}")
    print("Source left intact (read-only fallback).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
