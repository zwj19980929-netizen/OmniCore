"""
Anti-bot domain profile (B2).

Keeps a per-domain record of how hostile the site has been recently:

- ``block_count`` / ``request_count`` → block rate
- ``last_block_at`` / ``last_block_kind`` → recency + nature of the block
- ``preferred_ua`` → UA that has worked (or was last chosen for) the domain
- ``headed`` hint → whether headless mode kept getting blocked

Given the profile, ``suggest_throttle(domain)`` returns an adaptive
``ThrottleHint`` with a delay seconds value, a UA choice, and a "prefer
headed" flag. The hint is advisory — callers may or may not honour it.

Inactive when ``ANTI_BOT_PROFILE_ENABLED=false``.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from config.settings import settings
from utils.logger import log_warning


BLOCK_KINDS = ("captcha", "rate_limit", "honeypot", "service_unavailable", "unknown")


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS anti_bot_domains (
        domain TEXT PRIMARY KEY,
        request_count INTEGER NOT NULL DEFAULT 0,
        success_count INTEGER NOT NULL DEFAULT 0,
        block_count INTEGER NOT NULL DEFAULT 0,
        consecutive_success INTEGER NOT NULL DEFAULT 0,
        last_block_at TEXT,
        last_block_kind TEXT,
        last_request_at TEXT,
        preferred_ua TEXT,
        prefers_headed INTEGER NOT NULL DEFAULT 0,
        current_delay_sec REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_antibot_last_block ON anti_bot_domains (last_block_at)",
)


@dataclass
class ThrottleHint:
    delay_sec: float = 0.0
    ua: str = ""
    headed: bool = False
    reason: str = ""
    block_rate: float = 0.0
    recent_block_kind: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "delay_sec": round(self.delay_sec, 3),
            "ua": self.ua,
            "headed": bool(self.headed),
            "reason": self.reason,
            "block_rate": round(self.block_rate, 4),
            "recent_block_kind": self.recent_block_kind,
        }


@dataclass
class DomainProfile:
    domain: str = ""
    request_count: int = 0
    success_count: int = 0
    block_count: int = 0
    consecutive_success: int = 0
    last_block_at: Optional[str] = None
    last_block_kind: Optional[str] = None
    preferred_ua: str = ""
    prefers_headed: bool = False
    current_delay_sec: float = 0.0

    @property
    def block_rate(self) -> float:
        if self.request_count <= 0:
            return 0.0
        return min(1.0, self.block_count / self.request_count)


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


# ----------------------------------------------------------------------
# UA pool
# ----------------------------------------------------------------------

_UA_POOL_CACHE: Optional[Dict[str, List[str]]] = None
_UA_POOL_LOCK = threading.Lock()


def _load_ua_pool() -> Dict[str, List[str]]:
    """Load ``config/ua_pool.yaml`` grouped by platform. Cached in-process.

    Returns a mapping like ``{"desktop_chrome": [ua1, ua2], ...}``. Never
    raises — on any error, returns an empty dict.
    """
    global _UA_POOL_CACHE
    if _UA_POOL_CACHE is not None:
        return _UA_POOL_CACHE
    with _UA_POOL_LOCK:
        if _UA_POOL_CACHE is not None:
            return _UA_POOL_CACHE
        path = Path(settings.ANTI_BOT_UA_POOL_FILE)
        if not path.is_absolute():
            path = settings.PROJECT_ROOT / path
        pool: Dict[str, List[str]] = {}
        try:
            import yaml  # type: ignore
            with path.open("r", encoding="utf-8") as handle:
                raw = yaml.safe_load(handle) or {}
            if isinstance(raw, dict):
                for group, values in raw.items():
                    if isinstance(values, list):
                        items = [str(v).strip() for v in values if str(v).strip()]
                        if items:
                            pool[str(group)] = items
        except (OSError, ImportError, ValueError) as exc:
            log_warning(f"UA pool load failed ({path}): {exc}")
        _UA_POOL_CACHE = pool
        return pool


def pick_ua(group: str = "desktop_chrome") -> str:
    """Return one UA string from the pool; empty string if pool is empty."""
    pool = _load_ua_pool()
    items = pool.get(group) or []
    if not items:
        # fall back to first non-empty group
        for values in pool.values():
            if values:
                items = values
                break
    if not items:
        return ""
    # Stable choice: first item (callers can rotate by passing different group)
    return items[0]


# ----------------------------------------------------------------------
# Profile store
# ----------------------------------------------------------------------


class AntiBotProfileStore:
    """Thread-safe SQLite store for domain profiles."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = Path(db_path) if db_path else Path(settings.ANTI_BOT_PROFILE_DB)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    # ---- connection ----

    def _ensure_conn(self) -> Optional[sqlite3.Connection]:
        if not settings.ANTI_BOT_PROFILE_ENABLED:
            return None
        if self._conn is not None:
            return self._conn
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            for stmt in _SCHEMA:
                conn.execute(stmt)
            conn.commit()
            self._conn = conn
            return conn
        except sqlite3.Error as exc:
            log_warning(f"AntiBotProfileStore init failed: {exc}")
            return None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # ---- internal helpers ----

    def _row_to_profile(self, row: sqlite3.Row) -> DomainProfile:
        return DomainProfile(
            domain=row["domain"],
            request_count=int(row["request_count"] or 0),
            success_count=int(row["success_count"] or 0),
            block_count=int(row["block_count"] or 0),
            consecutive_success=int(row["consecutive_success"] or 0),
            last_block_at=row["last_block_at"],
            last_block_kind=row["last_block_kind"],
            preferred_ua=row["preferred_ua"] or "",
            prefers_headed=bool(int(row["prefers_headed"] or 0)),
            current_delay_sec=float(row["current_delay_sec"] or 0.0),
        )

    def _ensure_row(self, conn: sqlite3.Connection, domain: str) -> None:
        now = _now_iso()
        conn.execute(
            """
            INSERT OR IGNORE INTO anti_bot_domains
                (domain, created_at, updated_at)
            VALUES (?, ?, ?)
            """,
            (domain, now, now),
        )

    # ---- write path ----

    def record_request(self, domain_or_url: str, success: bool) -> None:
        """Increment request/success counters after a page load attempt."""
        domain = normalize_domain(domain_or_url)
        if not domain:
            return
        now = _now_iso()
        cooldown = int(settings.ANTI_BOT_SUCCESS_TO_COOLDOWN)
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return
            try:
                self._ensure_row(conn, domain)
                if success:
                    conn.execute(
                        """
                        UPDATE anti_bot_domains SET
                            request_count = request_count + 1,
                            success_count = success_count + 1,
                            consecutive_success = consecutive_success + 1,
                            last_request_at = ?,
                            updated_at = ?,
                            current_delay_sec = CASE
                                WHEN consecutive_success + 1 >= ? THEN 0
                                ELSE current_delay_sec
                            END
                        WHERE domain = ?
                        """,
                        (now, now, cooldown, domain),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE anti_bot_domains SET
                            request_count = request_count + 1,
                            consecutive_success = 0,
                            last_request_at = ?,
                            updated_at = ?
                        WHERE domain = ?
                        """,
                        (now, now, domain),
                    )
                conn.commit()
            except sqlite3.Error as exc:
                log_warning(f"record_request failed: {exc}")

    def record_block(
        self,
        domain_or_url: str,
        kind: str = "unknown",
    ) -> None:
        """Record that ``domain`` blocked us; bumps delay for the next visit."""
        domain = normalize_domain(domain_or_url)
        if not domain:
            return
        kind_clean = (kind or "unknown").strip().lower()
        if kind_clean not in BLOCK_KINDS:
            kind_clean = "unknown"
        now = _now_iso()
        initial_delay = float(settings.ANTI_BOT_INITIAL_DELAY_SEC)
        max_delay = float(settings.ANTI_BOT_MAX_DELAY_SEC)
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return
            try:
                self._ensure_row(conn, domain)
                row = conn.execute(
                    "SELECT current_delay_sec FROM anti_bot_domains WHERE domain = ?",
                    (domain,),
                ).fetchone()
                # Exponential back-off starting from initial_delay (minimum 1s when
                # initial is zero), capped at max_delay.
                current = float(row["current_delay_sec"] or 0.0) if row else 0.0
                base = max(current * 2, initial_delay if initial_delay > 0 else 1.0)
                new_delay = min(base, max_delay)
                conn.execute(
                    """
                    UPDATE anti_bot_domains SET
                        request_count = request_count + 1,
                        block_count = block_count + 1,
                        consecutive_success = 0,
                        last_block_at = ?,
                        last_block_kind = ?,
                        current_delay_sec = ?,
                        prefers_headed = 1,
                        last_request_at = ?,
                        updated_at = ?
                    WHERE domain = ?
                    """,
                    (now, kind_clean, new_delay, now, now, domain),
                )
                conn.commit()
            except sqlite3.Error as exc:
                log_warning(f"record_block failed: {exc}")

    def set_preferred_ua(self, domain_or_url: str, ua: str) -> None:
        domain = normalize_domain(domain_or_url)
        ua_clean = (ua or "").strip()
        if not domain or not ua_clean:
            return
        now = _now_iso()
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return
            try:
                self._ensure_row(conn, domain)
                conn.execute(
                    """
                    UPDATE anti_bot_domains SET
                        preferred_ua = ?,
                        updated_at = ?
                    WHERE domain = ?
                    """,
                    (ua_clean, now, domain),
                )
                conn.commit()
            except sqlite3.Error as exc:
                log_warning(f"set_preferred_ua failed: {exc}")

    # ---- read path ----

    def get_profile(self, domain_or_url: str) -> Optional[DomainProfile]:
        domain = normalize_domain(domain_or_url)
        if not domain:
            return None
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return None
            try:
                row = conn.execute(
                    "SELECT * FROM anti_bot_domains WHERE domain = ?",
                    (domain,),
                ).fetchone()
            except sqlite3.Error as exc:
                log_warning(f"get_profile failed: {exc}")
                return None
        if row is None:
            return None
        return self._row_to_profile(row)

    def suggest_throttle(self, domain_or_url: str) -> ThrottleHint:
        """Return an advisory throttle hint for the next request to ``domain``.

        Decisions:
        - ``delay_sec``: current stored delay, after applying time-decay when
          the last block is older than ``ANTI_BOT_BLOCK_DECAY_DAYS``.
        - ``headed``: True if the domain has ever blocked us (``prefers_headed``).
        - ``ua``: preferred UA when recorded; otherwise the first UA from the pool.
        - Empty hint when disabled or no profile exists.
        """
        hint = ThrottleHint()
        if not settings.ANTI_BOT_PROFILE_ENABLED:
            return hint
        profile = self.get_profile(domain_or_url)
        if profile is None:
            # Still fill UA from pool for bootstrap
            hint.ua = pick_ua()
            hint.reason = "no_profile"
            return hint

        # Time decay on last block
        decay_days = int(settings.ANTI_BOT_BLOCK_DECAY_DAYS)
        last_block = _parse_iso(profile.last_block_at)
        delay = float(profile.current_delay_sec or 0.0)
        if last_block:
            age_days = (datetime.now() - last_block).total_seconds() / 86400.0
            if age_days > decay_days:
                # Past the decay window — wind delay down to zero
                delay = 0.0

        hint.delay_sec = max(
            0.0, min(delay, float(settings.ANTI_BOT_MAX_DELAY_SEC))
        )
        hint.headed = profile.prefers_headed
        hint.ua = profile.preferred_ua or pick_ua()
        hint.block_rate = profile.block_rate
        hint.recent_block_kind = profile.last_block_kind or ""
        if hint.delay_sec > 0 or hint.headed:
            hint.reason = f"blocks={profile.block_count} rate={profile.block_rate:.2f}"
        else:
            hint.reason = "profile_cool"
        return hint

    # ---- maintenance ----

    def list_profiles(self, *, limit: int = 50) -> List[DomainProfile]:
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return []
            try:
                rows = conn.execute(
                    """
                    SELECT * FROM anti_bot_domains
                    ORDER BY block_count DESC, request_count DESC
                    LIMIT ?
                    """,
                    (max(int(limit), 1),),
                ).fetchall()
            except sqlite3.Error as exc:
                log_warning(f"list_profiles failed: {exc}")
                return []
        return [self._row_to_profile(row) for row in rows]


_ANTIBOT_SINGLETON: Optional[AntiBotProfileStore] = None
_ANTIBOT_SINGLETON_LOCK = threading.Lock()


def get_anti_bot_profile_store() -> Optional[AntiBotProfileStore]:
    if not settings.ANTI_BOT_PROFILE_ENABLED:
        return None
    global _ANTIBOT_SINGLETON
    if _ANTIBOT_SINGLETON is not None:
        return _ANTIBOT_SINGLETON
    with _ANTIBOT_SINGLETON_LOCK:
        if _ANTIBOT_SINGLETON is None:
            _ANTIBOT_SINGLETON = AntiBotProfileStore()
        return _ANTIBOT_SINGLETON
