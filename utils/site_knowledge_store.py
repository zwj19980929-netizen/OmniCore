"""
Site knowledge store (B1).

Lightweight SQLite cache that remembers, per-domain:

1. Element selectors that have proved to work (click / input / submit targets),
   so the next visit can try them before falling back to LLM-led discovery.
2. Login flows that succeeded end-to-end, so re-login can be replayed
   without re-asking the LLM to plan.
3. Reusable action-sequence templates (e.g. search, paginate, filter).

All three are *augmentations*, not replacements: the browser agent still
owns the decision, this store just surfaces evidence from prior runs.

The store is a no-op when ``BROWSER_PLAN_MEMORY_ENABLED=false``.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from config.settings import settings
from utils.logger import log_warning


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS site_selectors (
        domain TEXT NOT NULL,
        role TEXT NOT NULL,
        selector TEXT NOT NULL,
        hit_count INTEGER NOT NULL DEFAULT 0,
        fail_count INTEGER NOT NULL DEFAULT 0,
        last_used_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (domain, role, selector)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS site_login_flows (
        domain TEXT PRIMARY KEY,
        flow_json TEXT NOT NULL,
        auth_type TEXT NOT NULL DEFAULT 'password',
        last_success_at TEXT,
        last_failure_at TEXT,
        fail_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS site_action_templates (
        domain TEXT NOT NULL,
        template_name TEXT NOT NULL,
        sequence_json TEXT NOT NULL,
        hit_count INTEGER NOT NULL DEFAULT 0,
        last_used_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (domain, template_name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_selectors_domain_role ON site_selectors (domain, role)",
    "CREATE INDEX IF NOT EXISTS idx_templates_domain ON site_action_templates (domain)",
)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def normalize_domain(url_or_host: str) -> str:
    """Return the registered domain (host) for a URL or raw host string.

    - ``https://login.acme.com/path`` → ``login.acme.com``
    - ``acme.com`` → ``acme.com``
    - ``""`` → ``""``
    """
    text = (url_or_host or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    try:
        host = urlparse(text).hostname or ""
    except ValueError:
        return ""
    return host.lower()


class SiteKnowledgeStore:
    """Thread-safe SQLite wrapper with lazy initialization."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = Path(db_path) if db_path else Path(settings.BROWSER_SITE_KNOWLEDGE_DB)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # connection management
    # ------------------------------------------------------------------

    def _ensure_conn(self) -> Optional[sqlite3.Connection]:
        if not settings.BROWSER_PLAN_MEMORY_ENABLED:
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
            log_warning(f"SiteKnowledgeStore init failed: {exc}")
            return None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # ------------------------------------------------------------------
    # selectors
    # ------------------------------------------------------------------

    def record_selector_success(
        self,
        domain_or_url: str,
        role: str,
        selector: str,
    ) -> bool:
        """Upsert a (domain, role, selector) row and bump ``hit_count``."""
        domain = normalize_domain(domain_or_url)
        role = (role or "").strip().lower()
        selector = (selector or "").strip()
        if not domain or not role or not selector:
            return False
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return False
            try:
                now = _now_iso()
                conn.execute(
                    """
                    INSERT INTO site_selectors
                        (domain, role, selector, hit_count, fail_count, last_used_at, created_at)
                    VALUES (?, ?, ?, 1, 0, ?, ?)
                    ON CONFLICT(domain, role, selector) DO UPDATE SET
                        hit_count = hit_count + 1,
                        last_used_at = excluded.last_used_at
                    """,
                    (domain, role, selector, now, now),
                )
                conn.commit()
                return True
            except sqlite3.Error as exc:
                log_warning(f"record_selector_success failed: {exc}")
                return False

    def record_selector_failure(
        self,
        domain_or_url: str,
        role: str,
        selector: str,
    ) -> bool:
        domain = normalize_domain(domain_or_url)
        role = (role or "").strip().lower()
        selector = (selector or "").strip()
        if not domain or not role or not selector:
            return False
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return False
            try:
                now = _now_iso()
                conn.execute(
                    """
                    INSERT INTO site_selectors
                        (domain, role, selector, hit_count, fail_count, last_used_at, created_at)
                    VALUES (?, ?, ?, 0, 1, ?, ?)
                    ON CONFLICT(domain, role, selector) DO UPDATE SET
                        fail_count = fail_count + 1,
                        last_used_at = excluded.last_used_at
                    """,
                    (domain, role, selector, now, now),
                )
                conn.commit()
                return True
            except sqlite3.Error as exc:
                log_warning(f"record_selector_failure failed: {exc}")
                return False

    def get_selector_hints(
        self,
        domain_or_url: str,
        *,
        role: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return selectors worth trying, filtered by min success rate and decay.

        Rows below ``BROWSER_SELECTOR_MIN_SUCCESS_RATE`` or unused for more
        than ``BROWSER_SELECTOR_DECAY_DAYS`` days are dropped.
        """
        domain = normalize_domain(domain_or_url)
        if not domain:
            return []
        top_k = int(limit if limit is not None else settings.BROWSER_SELECTOR_HINT_TOP_K)
        min_rate = float(settings.BROWSER_SELECTOR_MIN_SUCCESS_RATE)
        decay_days = int(settings.BROWSER_SELECTOR_DECAY_DAYS)
        cutoff = datetime.now() - timedelta(days=max(decay_days, 1))
        role_filter = (role or "").strip().lower()

        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return []
            try:
                if role_filter:
                    rows = conn.execute(
                        """
                        SELECT domain, role, selector, hit_count, fail_count, last_used_at
                        FROM site_selectors
                        WHERE domain = ? AND role = ?
                        """,
                        (domain, role_filter),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT domain, role, selector, hit_count, fail_count, last_used_at
                        FROM site_selectors
                        WHERE domain = ?
                        """,
                        (domain,),
                    ).fetchall()
            except sqlite3.Error as exc:
                log_warning(f"get_selector_hints failed: {exc}")
                return []

        hints: List[Dict[str, Any]] = []
        for row in rows:
            hits = int(row["hit_count"] or 0)
            fails = int(row["fail_count"] or 0)
            total = hits + fails
            if total == 0:
                continue
            rate = hits / total
            if rate < min_rate:
                continue
            last_used = _parse_iso(row["last_used_at"])
            if last_used and last_used < cutoff:
                continue
            hints.append(
                {
                    "domain": row["domain"],
                    "role": row["role"],
                    "selector": row["selector"],
                    "hit_count": hits,
                    "fail_count": fails,
                    "success_rate": rate,
                    "last_used_at": row["last_used_at"],
                }
            )
        hints.sort(key=lambda h: (h["success_rate"], h["hit_count"]), reverse=True)
        return hints[: max(top_k, 0)]

    # ------------------------------------------------------------------
    # login flows
    # ------------------------------------------------------------------

    def record_login_flow(
        self,
        domain_or_url: str,
        *,
        flow: Any,
        auth_type: str = "password",
        success: bool = True,
    ) -> bool:
        """Upsert a login flow template for ``domain``.

        ``flow`` is serialized to JSON — usually a list of executed steps
        (action + target hint). ``success=True`` resets ``fail_count``;
        ``success=False`` increments ``fail_count`` and records the failure
        timestamp without overwriting ``flow_json``.
        """
        domain = normalize_domain(domain_or_url)
        if not domain:
            return False
        try:
            serialized = json.dumps(flow, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return False
        now = _now_iso()
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return False
            try:
                existing = conn.execute(
                    "SELECT flow_json, fail_count FROM site_login_flows WHERE domain = ?",
                    (domain,),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO site_login_flows
                            (domain, flow_json, auth_type, last_success_at,
                             last_failure_at, fail_count, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            domain,
                            serialized,
                            auth_type or "password",
                            now if success else None,
                            None if success else now,
                            0 if success else 1,
                            now,
                            now,
                        ),
                    )
                else:
                    if success:
                        conn.execute(
                            """
                            UPDATE site_login_flows SET
                                flow_json = ?,
                                auth_type = ?,
                                last_success_at = ?,
                                fail_count = 0,
                                updated_at = ?
                            WHERE domain = ?
                            """,
                            (serialized, auth_type or "password", now, now, domain),
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE site_login_flows SET
                                last_failure_at = ?,
                                fail_count = fail_count + 1,
                                updated_at = ?
                            WHERE domain = ?
                            """,
                            (now, now, domain),
                        )
                conn.commit()
                return True
            except sqlite3.Error as exc:
                log_warning(f"record_login_flow failed: {exc}")
                return False

    def get_login_flow(self, domain_or_url: str) -> Optional[Dict[str, Any]]:
        domain = normalize_domain(domain_or_url)
        if not domain:
            return None
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return None
            try:
                row = conn.execute(
                    """
                    SELECT domain, flow_json, auth_type, last_success_at,
                           last_failure_at, fail_count, created_at, updated_at
                    FROM site_login_flows WHERE domain = ?
                    """,
                    (domain,),
                ).fetchone()
            except sqlite3.Error as exc:
                log_warning(f"get_login_flow failed: {exc}")
                return None
        if row is None:
            return None
        try:
            flow = json.loads(row["flow_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            flow = None
        return {
            "domain": row["domain"],
            "flow": flow,
            "auth_type": row["auth_type"],
            "last_success_at": row["last_success_at"],
            "last_failure_at": row["last_failure_at"],
            "fail_count": int(row["fail_count"] or 0),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ------------------------------------------------------------------
    # action templates
    # ------------------------------------------------------------------

    def record_template(
        self,
        domain_or_url: str,
        template_name: str,
        sequence: Any,
    ) -> bool:
        domain = normalize_domain(domain_or_url)
        name = (template_name or "").strip()
        if not domain or not name:
            return False
        try:
            serialized = json.dumps(sequence, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return False
        now = _now_iso()
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return False
            try:
                conn.execute(
                    """
                    INSERT INTO site_action_templates
                        (domain, template_name, sequence_json, hit_count, last_used_at, created_at)
                    VALUES (?, ?, ?, 1, ?, ?)
                    ON CONFLICT(domain, template_name) DO UPDATE SET
                        sequence_json = excluded.sequence_json,
                        hit_count = hit_count + 1,
                        last_used_at = excluded.last_used_at
                    """,
                    (domain, name, serialized, now, now),
                )
                conn.commit()
                return True
            except sqlite3.Error as exc:
                log_warning(f"record_template failed: {exc}")
                return False

    def get_templates(
        self,
        domain_or_url: str,
        *,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        domain = normalize_domain(domain_or_url)
        if not domain:
            return []
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return []
            try:
                rows = conn.execute(
                    """
                    SELECT template_name, sequence_json, hit_count, last_used_at
                    FROM site_action_templates
                    WHERE domain = ?
                    ORDER BY hit_count DESC, last_used_at DESC
                    LIMIT ?
                    """,
                    (domain, max(int(limit), 1)),
                ).fetchall()
            except sqlite3.Error as exc:
                log_warning(f"get_templates failed: {exc}")
                return []
        out: List[Dict[str, Any]] = []
        for row in rows:
            try:
                sequence = json.loads(row["sequence_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                sequence = None
            out.append(
                {
                    "template_name": row["template_name"],
                    "sequence": sequence,
                    "hit_count": int(row["hit_count"] or 0),
                    "last_used_at": row["last_used_at"],
                }
            )
        return out


# Process-wide singleton for convenience
_SINGLETON: Optional[SiteKnowledgeStore] = None
_SINGLETON_LOCK = threading.Lock()


def get_site_knowledge_store() -> Optional[SiteKnowledgeStore]:
    """Return the shared ``SiteKnowledgeStore`` when enabled, else None."""
    if not settings.BROWSER_PLAN_MEMORY_ENABLED:
        return None
    global _SINGLETON
    if _SINGLETON is not None:
        return _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = SiteKnowledgeStore()
        return _SINGLETON
