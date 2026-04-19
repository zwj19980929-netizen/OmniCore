"""
Strategy stats store (B5).

Per-domain / per-role success rate for each fallback strategy the browser
execution layer tries (click / input / ...). Allows the fallback chain to
reorder toward strategies that have historically worked on that site and
skip strategies that are demonstrably useless.

Disabled (full no-op) when ``BROWSER_STRATEGY_LEARNING_ENABLED=false``.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from config.settings import settings
from utils.logger import log_warning
from utils.site_knowledge_store import normalize_domain


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS strategy_stats (
        domain TEXT NOT NULL,
        role TEXT NOT NULL,
        strategy TEXT NOT NULL,
        success_count INTEGER NOT NULL DEFAULT 0,
        fail_count INTEGER NOT NULL DEFAULT 0,
        total_latency_ms INTEGER NOT NULL DEFAULT 0,
        last_used_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (domain, role, strategy)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_strategy_domain_role ON strategy_stats (domain, role)",
)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class StrategyStatsStore:
    """Thread-safe SQLite wrapper with lazy initialization."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = Path(db_path) if db_path else Path(settings.BROWSER_STRATEGY_DB)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    def _ensure_conn(self) -> Optional[sqlite3.Connection]:
        if not settings.BROWSER_STRATEGY_LEARNING_ENABLED:
            return None
        if self._conn is not None:
            return self._conn
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            for stmt in _SCHEMA_STATEMENTS:
                conn.execute(stmt)
            conn.commit()
            self._conn = conn
            return conn
        except sqlite3.Error as exc:
            log_warning(f"StrategyStatsStore init failed: {exc}")
            return None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # ------------------------------------------------------------------
    # record
    # ------------------------------------------------------------------

    def record(
        self,
        domain_or_url: str,
        role: str,
        strategy: str,
        *,
        success: bool,
        latency_ms: int = 0,
    ) -> bool:
        domain = normalize_domain(domain_or_url)
        role_key = (role or "").strip().lower()
        strategy_key = (strategy or "").strip().lower()
        if not domain or not role_key or not strategy_key:
            return False
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return False
            try:
                now = _now_iso()
                succ_delta = 1 if success else 0
                fail_delta = 0 if success else 1
                lat = max(int(latency_ms or 0), 0)
                conn.execute(
                    """
                    INSERT INTO strategy_stats
                        (domain, role, strategy, success_count, fail_count,
                         total_latency_ms, last_used_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(domain, role, strategy) DO UPDATE SET
                        success_count = success_count + excluded.success_count,
                        fail_count    = fail_count + excluded.fail_count,
                        total_latency_ms = total_latency_ms + excluded.total_latency_ms,
                        last_used_at  = excluded.last_used_at
                    """,
                    (
                        domain,
                        role_key,
                        strategy_key,
                        succ_delta,
                        fail_delta,
                        lat,
                        now,
                        now,
                    ),
                )
                conn.commit()
                return True
            except sqlite3.Error as exc:
                log_warning(f"strategy_stats.record failed: {exc}")
                return False

    # ------------------------------------------------------------------
    # query
    # ------------------------------------------------------------------

    def _load_rows(self, domain: str, role_key: str) -> List[sqlite3.Row]:
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return []
            try:
                return conn.execute(
                    """
                    SELECT strategy, success_count, fail_count, total_latency_ms,
                           last_used_at
                    FROM strategy_stats
                    WHERE domain = ? AND role = ?
                    """,
                    (domain, role_key),
                ).fetchall()
            except sqlite3.Error as exc:
                log_warning(f"strategy_stats load failed: {exc}")
                return []

    def get_stats(
        self,
        domain_or_url: str,
        role: str,
    ) -> Dict[str, Dict[str, float]]:
        """Return raw per-strategy stats for diagnostics/testing."""
        domain = normalize_domain(domain_or_url)
        role_key = (role or "").strip().lower()
        if not domain or not role_key:
            return {}
        out: Dict[str, Dict[str, float]] = {}
        for row in self._load_rows(domain, role_key):
            succ = int(row["success_count"] or 0)
            fail = int(row["fail_count"] or 0)
            total = succ + fail
            out[row["strategy"]] = {
                "success_count": succ,
                "fail_count": fail,
                "total": total,
                "success_rate": (succ / total) if total else 0.0,
                "avg_latency_ms": (int(row["total_latency_ms"] or 0) / total) if total else 0.0,
                "last_used_at": row["last_used_at"],
            }
        return out

    def ranked_strategies(
        self,
        domain_or_url: str,
        role: str,
    ) -> List[str]:
        """Return strategies ordered by observed success rate (desc).

        Only strategies with ``total >= BROWSER_STRATEGY_MIN_SAMPLES`` are
        included; callers treat absent strategies as "use default order".
        Strategies whose success_rate is below
        ``BROWSER_STRATEGY_SKIP_THRESHOLD`` are omitted as well — they belong
        in ``skip_strategies()``.
        """
        domain = normalize_domain(domain_or_url)
        role_key = (role or "").strip().lower()
        if not domain or not role_key:
            return []
        min_samples = max(int(settings.BROWSER_STRATEGY_MIN_SAMPLES), 1)
        skip_rate = float(settings.BROWSER_STRATEGY_SKIP_THRESHOLD)
        scored: List[tuple] = []
        for row in self._load_rows(domain, role_key):
            succ = int(row["success_count"] or 0)
            fail = int(row["fail_count"] or 0)
            total = succ + fail
            if total < min_samples:
                continue
            rate = succ / total
            if rate < skip_rate:
                continue
            scored.append((rate, succ, row["strategy"]))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [s for _, _, s in scored]

    def skip_strategies(
        self,
        domain_or_url: str,
        role: str,
    ) -> Set[str]:
        """Return strategies with observed success rate below skip threshold."""
        domain = normalize_domain(domain_or_url)
        role_key = (role or "").strip().lower()
        if not domain or not role_key:
            return set()
        min_samples = max(int(settings.BROWSER_STRATEGY_MIN_SAMPLES), 1)
        skip_rate = float(settings.BROWSER_STRATEGY_SKIP_THRESHOLD)
        out: Set[str] = set()
        for row in self._load_rows(domain, role_key):
            succ = int(row["success_count"] or 0)
            fail = int(row["fail_count"] or 0)
            total = succ + fail
            if total < min_samples:
                continue
            if (succ / total) < skip_rate:
                out.add(row["strategy"])
        return out


# Process-wide singleton
_SINGLETON: Optional[StrategyStatsStore] = None
_SINGLETON_LOCK = threading.Lock()


def get_strategy_stats_store() -> Optional[StrategyStatsStore]:
    """Return the shared ``StrategyStatsStore`` when enabled, else None."""
    if not settings.BROWSER_STRATEGY_LEARNING_ENABLED:
        return None
    global _SINGLETON
    if _SINGLETON is not None:
        return _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = StrategyStatsStore()
        return _SINGLETON
