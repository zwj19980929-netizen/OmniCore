"""
Vision description cache (B3).

Persists ``page_hash → vision_description`` so revisiting visually-similar
pages can skip the vision-model round-trip and reuse a prior description.

The cache is a no-op when ``BROWSER_VISION_CACHE_ENABLED=false``. Entries
older than ``BROWSER_VISION_CACHE_TTL_DAYS`` are treated as misses (and
purged opportunistically on the next ``set``). Tasks that mention a
high-risk keyword (login/payment/...) bypass the cache entirely so the
visual is always fresh — see :func:`should_bypass_for_task`.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import settings
from utils.logger import log_warning


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS vision_cache (
        page_hash TEXT PRIMARY KEY,
        url_template TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL,
        hit_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        last_used_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_vision_cache_last_used ON vision_cache (last_used_at)",
)


def _now() -> datetime:
    return datetime.now()


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _encode_controls(controls: Optional[List[Dict[str, Any]]]) -> str:
    if not controls:
        return ""
    clean: List[Dict[str, str]] = []
    for item in controls:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        label = str(item.get("label") or "").strip()
        if not role and not label:
            continue
        clean.append({"role": role, "label": label})
    if not clean:
        return ""
    try:
        return json.dumps(clean, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def _decode_controls(raw: Any) -> List[Dict[str, str]]:
    if not raw:
        return []
    try:
        parsed = json.loads(str(raw))
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    out: List[Dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        label = str(item.get("label") or "").strip()
        if not role and not label:
            continue
        out.append({"role": role, "label": label})
    return out


@dataclass
class CachedVision:
    page_hash: str
    description: str
    url_template: str
    hit_count: int
    created_at: str
    last_used_at: str
    controls: List[Dict[str, str]] = field(default_factory=list)


def should_bypass_for_task(task: str) -> bool:
    """Return True if the task contains a configured bypass keyword.

    Matching is case-insensitive substring. Empty keyword list disables
    bypass altogether.
    """
    if not task:
        return False
    raw = settings.BROWSER_VISION_CACHE_BYPASS_KEYWORDS or ""
    keywords = [
        token.strip().lower()
        for token in str(raw).replace(";", ",").split(",")
        if token.strip()
    ]
    if not keywords:
        return False
    haystack = task.lower()
    return any(kw in haystack for kw in keywords)


class VisionCache:
    """Thread-safe SQLite-backed vision description cache."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = Path(db_path) if db_path else Path(settings.BROWSER_VISION_CACHE_DB)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------

    def _ensure_conn(self) -> Optional[sqlite3.Connection]:
        if not settings.BROWSER_VISION_CACHE_ENABLED:
            return None
        if self._conn is not None:
            return self._conn
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            for stmt in _SCHEMA:
                conn.execute(stmt)
            # Backward-compat: add controls_json column to legacy DBs.
            try:
                cols = {row["name"] for row in conn.execute("PRAGMA table_info(vision_cache)")}
                if "controls_json" not in cols:
                    conn.execute("ALTER TABLE vision_cache ADD COLUMN controls_json TEXT NOT NULL DEFAULT ''")
            except sqlite3.Error as exc:
                log_warning(f"VisionCache controls migration failed: {exc}")
            conn.commit()
            self._conn = conn
            return conn
        except sqlite3.Error as exc:
            log_warning(f"VisionCache init failed: {exc}")
            return None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # ------------------------------------------------------------------
    # api
    # ------------------------------------------------------------------

    def get(self, page_hash: str) -> Optional[CachedVision]:
        """Return cached description if fresh, else None.

        Side effects on hit: ``hit_count += 1`` and ``last_used_at`` bumped
        so frequently-used templates resist expiry under TTL.
        """
        ph = (page_hash or "").strip()
        if not ph:
            return None
        ttl_days = max(int(settings.BROWSER_VISION_CACHE_TTL_DAYS), 1)
        cutoff = _now() - timedelta(days=ttl_days)
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return None
            try:
                row = conn.execute(
                    """
                    SELECT page_hash, url_template, description, hit_count,
                           created_at, last_used_at, controls_json
                    FROM vision_cache WHERE page_hash = ?
                    """,
                    (ph,),
                ).fetchone()
                if row is None:
                    return None
                created = _parse_iso(row["created_at"])
                if created and created < cutoff:
                    return None
                now = _now_iso()
                conn.execute(
                    """
                    UPDATE vision_cache SET
                        hit_count = hit_count + 1,
                        last_used_at = ?
                    WHERE page_hash = ?
                    """,
                    (now, ph),
                )
                conn.commit()
                controls = _decode_controls(row["controls_json"] if "controls_json" in row.keys() else "")
                return CachedVision(
                    page_hash=row["page_hash"],
                    description=row["description"],
                    url_template=row["url_template"] or "",
                    hit_count=int(row["hit_count"] or 0) + 1,
                    created_at=row["created_at"],
                    last_used_at=now,
                    controls=controls,
                )
            except sqlite3.Error as exc:
                log_warning(f"VisionCache.get failed: {exc}")
                return None

    def set(
        self,
        page_hash: str,
        description: str,
        url_template: str = "",
        controls: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """Upsert a description + optional structured controls list."""
        ph = (page_hash or "").strip()
        desc = (description or "").strip()
        if not ph or not desc:
            return False
        controls_json = _encode_controls(controls)
        now = _now_iso()
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return False
            try:
                conn.execute(
                    """
                    INSERT INTO vision_cache
                        (page_hash, url_template, description, hit_count,
                         created_at, last_used_at, controls_json)
                    VALUES (?, ?, ?, 0, ?, ?, ?)
                    ON CONFLICT(page_hash) DO UPDATE SET
                        url_template = excluded.url_template,
                        description = excluded.description,
                        last_used_at = excluded.last_used_at,
                        controls_json = excluded.controls_json
                    """,
                    (ph, url_template or "", desc, now, now, controls_json),
                )
                conn.commit()
                self._purge_expired_locked(conn)
                return True
            except sqlite3.Error as exc:
                log_warning(f"VisionCache.set failed: {exc}")
                return False

    def _purge_expired_locked(self, conn: sqlite3.Connection) -> None:
        """Best-effort TTL purge. Runs inside the caller's lock."""
        ttl_days = max(int(settings.BROWSER_VISION_CACHE_TTL_DAYS), 1)
        cutoff = (_now() - timedelta(days=ttl_days)).isoformat(timespec="seconds")
        try:
            conn.execute(
                "DELETE FROM vision_cache WHERE created_at < ?",
                (cutoff,),
            )
            conn.commit()
        except sqlite3.Error:
            pass

    def stats(self) -> dict:
        """Return ``{entries, total_hits}`` for diagnostics."""
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return {"entries": 0, "total_hits": 0}
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(hit_count), 0) AS h FROM vision_cache"
                ).fetchone()
                return {"entries": int(row["n"] or 0), "total_hits": int(row["h"] or 0)}
            except sqlite3.Error:
                return {"entries": 0, "total_hits": 0}


# ----------------------------------------------------------------------
# Process-wide singleton
# ----------------------------------------------------------------------

_SINGLETON: Optional[VisionCache] = None
_SINGLETON_LOCK = threading.Lock()


def get_vision_cache() -> Optional[VisionCache]:
    """Return shared :class:`VisionCache` when enabled, else None."""
    if not settings.BROWSER_VISION_CACHE_ENABLED:
        return None
    global _SINGLETON
    if _SINGLETON is not None:
        return _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = VisionCache()
        return _SINGLETON


def reset_singleton_for_tests() -> None:
    """Test hook: drop the cached singleton so a new DB path is honoured."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is not None:
            _SINGLETON.close()
        _SINGLETON = None
