"""
Tool Failure Auto-Tune (C2) — 通用 per-tool 失败画像。

为非 Browser 工具(file_worker / terminal_worker / web_worker / mcp tool ...)
提供与 B5 strategy_stats 同等粒度的成功率 / 失败模式统计：

- ``record_outcome``: tool_pipeline 末端调用,记录单次执行结果(success / error_type
  / latency)
- ``get_profile``: 返回滑动窗口内的聚合画像(success_rate / timeout_rate /
  error_tag 分布 / 平均延迟)
- ``get_recommendations``: 返回 router/planner 可直接消费的 hint 列表
  ("近期不可靠"/ "建议调高 timeout" / "倾向绕开")

设计要点:
- SQLite 单文件,events 表追加写,按 tool_name + 时间窗滚动查询
- 滑动窗口纯查询期完成,写入零开销
- ``TOOL_FAILURE_PROFILE_ENABLED=false`` 时全 no-op,主流程零负担
- 错误标签由 ``classify_error_tag`` 启发式得出,不调 LLM
"""
from __future__ import annotations

import re
import sqlite3
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import settings
from utils.logger import log_warning


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS tool_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tool_name TEXT NOT NULL,
        success INTEGER NOT NULL,
        error_tag TEXT NOT NULL DEFAULT '',
        latency_ms INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tool_events_tool ON tool_events (tool_name, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_tool_events_created ON tool_events (created_at)",
)


# ---------------------------------------------------------------------------
# Error tag classifier
# ---------------------------------------------------------------------------

_TAG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("timeout", re.compile(r"timeout|timed?\s*out|deadline", re.IGNORECASE)),
    ("rate_limit", re.compile(r"\b429\b|rate[\s_-]?limit|too\s+many\s+requests", re.IGNORECASE)),
    ("auth", re.compile(r"\b401\b|\b403\b|unauthor|forbidden|permission\s+denied|invalid\s+(?:api[_\s-]?key|token)", re.IGNORECASE)),
    ("parse_error", re.compile(r"json\s*decode|parse|malformed|invalid\s+json|schema", re.IGNORECASE)),
    ("network", re.compile(r"connection|network|dns|ssl|econnrefused|unreachable", re.IGNORECASE)),
    ("not_found", re.compile(r"\b404\b|not\s+found|no\s+such", re.IGNORECASE)),
    ("server_error", re.compile(r"\b5\d\d\b|server\s+error|internal\s+error", re.IGNORECASE)),
)


def classify_error_tag(error_type: Optional[str], error_message: Optional[str]) -> str:
    """Map raw error_type + message into a coarse bucket label.

    Returns ``"unknown"`` when nothing matches. Empty inputs → ``""``.
    """
    blob = " ".join(filter(None, (error_type or "", error_message or "")))
    if not blob.strip():
        return ""
    for tag, pattern in _TAG_PATTERNS:
        if pattern.search(blob):
            return tag
    return "unknown"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ToolFailureProfileStore:
    """Thread-safe SQLite wrapper. No-op when disabled."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = Path(db_path) if db_path else Path(settings.TOOL_FAILURE_PROFILE_DB)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    def _ensure_conn(self) -> Optional[sqlite3.Connection]:
        if not settings.TOOL_FAILURE_PROFILE_ENABLED:
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
            log_warning(f"ToolFailureProfileStore init failed: {exc}")
            return None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # ------------------------------------------------------------------
    # write
    # ------------------------------------------------------------------

    def record_outcome(
        self,
        tool_name: str,
        *,
        success: bool,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        latency_ms: int = 0,
    ) -> bool:
        name = (tool_name or "").strip()
        if not name:
            return False
        tag = "" if success else classify_error_tag(error_type, error_message)
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return False
            try:
                conn.execute(
                    "INSERT INTO tool_events (tool_name, success, error_tag, latency_ms, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (name, 1 if success else 0, tag, max(int(latency_ms or 0), 0), _now_iso()),
                )
                conn.commit()
                return True
            except sqlite3.Error as exc:
                log_warning(f"tool_failure_profile.record_outcome failed: {exc}")
                return False

    # ------------------------------------------------------------------
    # query
    # ------------------------------------------------------------------

    def _load_recent(self, tool_name: str, limit: int) -> List[sqlite3.Row]:
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return []
            try:
                return conn.execute(
                    "SELECT success, error_tag, latency_ms, created_at"
                    " FROM tool_events WHERE tool_name = ?"
                    " ORDER BY id DESC LIMIT ?",
                    (tool_name, max(int(limit), 1)),
                ).fetchall()
            except sqlite3.Error as exc:
                log_warning(f"tool_failure_profile load failed: {exc}")
                return []

    def get_profile(self, tool_name: str, *, window: Optional[int] = None) -> Dict[str, Any]:
        """Return aggregate stats over the last ``window`` events.

        Empty when no rows or store disabled. Caller should treat absent
        ``total`` as "no signal".
        """
        name = (tool_name or "").strip()
        if not name:
            return {}
        win = int(window) if window else int(settings.TOOL_FAILURE_WINDOW)
        rows = self._load_recent(name, win)
        if not rows:
            return {}
        total = len(rows)
        success_count = sum(1 for r in rows if int(r["success"] or 0) == 1)
        fail_count = total - success_count
        latencies = [int(r["latency_ms"] or 0) for r in rows]
        tag_counter: Counter[str] = Counter()
        last_success_at = ""
        last_failure_at = ""
        for r in rows:
            if int(r["success"] or 0) == 1:
                if not last_success_at:
                    last_success_at = r["created_at"]
            else:
                tag = (r["error_tag"] or "").strip() or "unknown"
                tag_counter[tag] += 1
                if not last_failure_at:
                    last_failure_at = r["created_at"]
        timeout_count = tag_counter.get("timeout", 0)
        return {
            "tool_name": name,
            "total": total,
            "success_count": success_count,
            "fail_count": fail_count,
            "success_rate": success_count / total,
            "fail_rate": fail_count / total,
            "timeout_rate": timeout_count / total,
            "avg_latency_ms": int(sum(latencies) / total) if latencies else 0,
            "error_tags": dict(tag_counter),
            "last_success_at": last_success_at,
            "last_failure_at": last_failure_at,
        }

    def get_all_profiles(self, *, window: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return profiles for every tool that has at least one event."""
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return []
            try:
                names = [
                    row["tool_name"]
                    for row in conn.execute(
                        "SELECT DISTINCT tool_name FROM tool_events"
                    ).fetchall()
                ]
            except sqlite3.Error as exc:
                log_warning(f"tool_failure_profile list failed: {exc}")
                return []
        return [p for name in names if (p := self.get_profile(name, window=window))]

    # ------------------------------------------------------------------
    # recommendations
    # ------------------------------------------------------------------

    def get_recommendation(self, tool_name: str) -> Optional[Dict[str, Any]]:
        """Return a single hint dict for the planner, or None when no signal.

        Hint shape:
          {
            "tool_name": ...,
            "level": "skip" | "warn" | "tune_timeout",
            "message": "...",
            "stats": {success_rate, timeout_rate, total, ...},
          }
        """
        profile = self.get_profile(tool_name)
        if not profile:
            return None
        min_samples = int(settings.TOOL_FAILURE_MIN_SAMPLES)
        if profile["total"] < min_samples:
            return None

        skip_rate = float(settings.TOOL_FAILURE_SKIP_THRESHOLD)
        warn_rate = float(settings.TOOL_FAILURE_WARN_THRESHOLD)
        timeout_rate = profile["timeout_rate"]
        fail_rate = profile["fail_rate"]
        top_tag = ""
        if profile["error_tags"]:
            top_tag = max(profile["error_tags"].items(), key=lambda kv: kv[1])[0]

        level: Optional[str] = None
        message = ""
        if timeout_rate >= skip_rate:
            level = "tune_timeout"
            message = (
                f"recent timeout_rate={timeout_rate:.0%} over last {profile['total']} "
                f"runs — raise timeout or pick another tool"
            )
        elif fail_rate >= skip_rate:
            level = "skip"
            message = (
                f"recent fail_rate={fail_rate:.0%} over last {profile['total']} runs"
            )
            if top_tag:
                message += f" (mostly {top_tag})"
            message += " — avoid unless necessary"
        elif fail_rate >= warn_rate:
            level = "warn"
            message = (
                f"recent fail_rate={fail_rate:.0%} over last {profile['total']} runs"
            )
            if top_tag:
                message += f" (mostly {top_tag})"
        if level is None:
            return None
        return {
            "tool_name": tool_name,
            "level": level,
            "message": message,
            "stats": {
                "total": profile["total"],
                "success_rate": round(profile["success_rate"], 3),
                "fail_rate": round(fail_rate, 3),
                "timeout_rate": round(timeout_rate, 3),
                "top_error_tag": top_tag,
                "avg_latency_ms": profile["avg_latency_ms"],
            },
        }

    def get_recommendations(self, *, top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return up to ``top_k`` non-empty hints across all tools.

        Sorted by severity (skip > tune_timeout > warn) then by sample size.
        """
        out: List[Dict[str, Any]] = []
        for profile in self.get_all_profiles():
            hint = self.get_recommendation(profile["tool_name"])
            if hint:
                out.append(hint)
        order = {"skip": 0, "tune_timeout": 1, "warn": 2}
        out.sort(key=lambda h: (order.get(h["level"], 9), -h["stats"]["total"]))
        if top_k is not None:
            out = out[: max(int(top_k), 1)]
        return out

    # ------------------------------------------------------------------
    # housekeeping
    # ------------------------------------------------------------------

    def purge(self, tool_name: Optional[str] = None) -> int:
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return 0
            try:
                if tool_name:
                    cur = conn.execute(
                        "DELETE FROM tool_events WHERE tool_name = ?", (tool_name,)
                    )
                else:
                    cur = conn.execute("DELETE FROM tool_events")
                conn.commit()
                return cur.rowcount or 0
            except sqlite3.Error as exc:
                log_warning(f"tool_failure_profile.purge failed: {exc}")
                return 0


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_SINGLETON: Optional[ToolFailureProfileStore] = None
_SINGLETON_LOCK = threading.Lock()


def get_tool_failure_profile_store() -> Optional[ToolFailureProfileStore]:
    """Return the shared store when feature enabled, else ``None``.

    Callers must treat ``None`` as "feature disabled" and short-circuit.
    """
    if not settings.TOOL_FAILURE_PROFILE_ENABLED:
        return None
    global _SINGLETON
    if _SINGLETON is not None:
        return _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = ToolFailureProfileStore()
        return _SINGLETON


def reset_tool_failure_profile_singleton_for_tests() -> None:
    """Test helper: drop cached singleton so a fresh DB path takes effect."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is not None:
            try:
                _SINGLETON.close()
            except Exception:
                pass
        _SINGLETON = None


def format_tool_health_block(hints: List[Dict[str, Any]]) -> str:
    """Render hints as a router-prompt markdown block. Empty in → empty out."""
    if not hints:
        return ""
    lines = ["## Recent tool health (avoid repeating known failures):"]
    for h in hints:
        marker = {
            "skip": "[avoid]",
            "tune_timeout": "[slow]",
            "warn": "[noisy]",
        }.get(h["level"], "[hint]")
        lines.append(f"- {marker} {h['tool_name']}: {h['message']}")
    return "\n".join(lines) + "\n\n---\n"


__all__ = [
    "ToolFailureProfileStore",
    "classify_error_tag",
    "format_tool_health_block",
    "get_tool_failure_profile_store",
    "reset_tool_failure_profile_singleton_for_tests",
]
