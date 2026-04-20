"""
Episodic Replay (C1) — 跨会话轨迹存储与召回。

把每次 Job 收口时的 task DAG 摘要落盘，新 Job 开工前按用户输入相似度召回
top-k 历史轨迹（success + failure 各取一条），拼成 episode_brief 注入 router
prompt，让 LLM 看到"过去这类任务别人是怎么走的"。

设计要点：
- SQLite 单文件，零外部依赖
- 检索用 token 重叠相似度（沿用 work_context_store 的轻量算法），不调 LLM
- 写入失败一律吞异常，主流程零影响
- 默认 off（EPISODE_REPLAY_ENABLED），冷启动空表期不打扰 router
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from config.settings import settings

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_signature TEXT NOT NULL,
    user_input TEXT NOT NULL,
    intent TEXT NOT NULL,
    plan_dag_json TEXT NOT NULL,
    outcome TEXT NOT NULL,
    cost_usd REAL DEFAULT 0,
    elapsed_ms INTEGER DEFAULT 0,
    llm_calls INTEGER DEFAULT 0,
    lessons TEXT DEFAULT '',
    job_id TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodes_signature ON episodes(task_signature);
CREATE INDEX IF NOT EXISTS idx_episodes_outcome ON episodes(outcome);
CREATE INDEX IF NOT EXISTS idx_episodes_created_at ON episodes(created_at);
"""

_STEP_DESC_LIMIT = 80
_INPUT_LIMIT = 240
_OUTPUT_LIMIT = 160
_LESSON_LIMIT = 600


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _short(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\n", " ").replace("\r", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)] + "…"


def _tokenize(text: str) -> set[str]:
    """Mirror utils.work_context_store._tokenize for cross-store comparability."""
    raw = str(text or "").lower()
    normalized: List[str] = []
    cjk_runs: List[str] = []
    current: List[str] = []
    for ch in raw:
        if "\u4e00" <= ch <= "\u9fff":
            current.append(ch)
            normalized.append(" ")
            continue
        if current:
            cjk_runs.append("".join(current))
            current = []
        normalized.append(ch if ch.isalnum() else " ")
    if current:
        cjk_runs.append("".join(current))

    tokens = {item for item in "".join(normalized).split() if len(item) >= 2}
    for run in cjk_runs:
        if len(run) == 1:
            tokens.add(run)
            continue
        max_n = min(3, len(run))
        for n in range(2, max_n + 1):
            for index in range(len(run) - n + 1):
                tokens.add(run[index : index + n])
    return tokens


def _token_overlap_score(query_tokens: set[str], candidate_tokens: set[str]) -> float:
    if not query_tokens or not candidate_tokens:
        return 0.0
    overlap = len(query_tokens & candidate_tokens)
    if overlap <= 0:
        return 0.0
    recall = overlap / max(len(query_tokens), 1)
    precision = overlap / max(len(candidate_tokens), 1)
    return (recall * 0.75) + (precision * 0.25)


def compute_task_signature(intent: str, user_input: str) -> str:
    """intent + 用户输入 token 集合的稳定 hash。

    Token 集合先排序再 hash，避免词序扰动。截短到 12 hex char 足够分桶。
    """
    intent_norm = (intent or "unknown").strip().lower()
    tokens = sorted(_tokenize(user_input))
    if tokens:
        digest = hashlib.sha1(("|".join(tokens)).encode("utf-8")).hexdigest()[:12]
    else:
        digest = "noinput"
    return f"{intent_norm}:{digest}"


def _classify_outcome(state: Dict[str, Any]) -> str:
    status = str(state.get("execution_status", "") or "").lower()
    critic_approved = bool(state.get("critic_approved", False))
    tasks = state.get("task_queue", []) or []
    if not tasks:
        # 没有 task 但 critic 通过 → 直接回答，不算 episode
        return ""
    failed = sum(1 for t in tasks if str(t.get("status", "")).lower() == "failed")
    completed = sum(1 for t in tasks if str(t.get("status", "")).lower() == "completed")
    if status == "completed" and critic_approved and failed == 0:
        return "success"
    if completed > 0 and failed > 0:
        return "partial"
    if failed > 0 or status in {"completed_with_issues", "blocked"}:
        return "fail"
    return ""


def _compress_plan_dag(
    task_queue: Iterable[Dict[str, Any]],
    *,
    max_steps: int = 8,
) -> List[Dict[str, str]]:
    """压缩 task DAG，每步 ≤ 80 字符可读摘要。"""
    compressed: List[Dict[str, str]] = []
    for task in task_queue:
        if not isinstance(task, dict):
            continue
        params = task.get("params") if isinstance(task.get("params"), dict) else {}
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        brief_input = ""
        for key in ("query", "url", "path", "command", "description", "input"):
            if key in params and params[key]:
                brief_input = _short(params[key], _INPUT_LIMIT)
                break
        if not brief_input:
            brief_input = _short(task.get("description", ""), _INPUT_LIMIT)

        brief_output = ""
        for key in ("summary", "answer", "content", "data", "url", "path"):
            if key in result and result[key]:
                brief_output = _short(result[key], _OUTPUT_LIMIT)
                break

        compressed.append(
            {
                "tool": _short(task.get("tool_name") or task.get("task_type") or "", 60),
                "intent": _short(task.get("description", ""), _STEP_DESC_LIMIT),
                "input": brief_input,
                "output": brief_output,
                "status": str(task.get("status", "") or ""),
            }
        )
        if len(compressed) >= max_steps:
            break
    return compressed


class EpisodeStore:
    """SQLite-backed episodic trace store.

    Thread-safe via a process-local lock; SQLite connection per-call to avoid
    pinning the writer thread.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        path = Path(db_path) if db_path else Path(settings.EPISODE_REPLAY_DB)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = path
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)

    # ------------------------------------------------------------------
    # write path
    # ------------------------------------------------------------------

    def record_episode(self, state: Dict[str, Any]) -> Optional[int]:
        """Persist one episode row from finalized job state.

        Returns inserted rowid, or None when nothing was written (no tasks /
        unknown outcome / disabled).
        """
        if not getattr(settings, "EPISODE_REPLAY_ENABLED", False):
            return None
        outcome = _classify_outcome(state)
        if not outcome:
            return None

        user_input = _short(state.get("user_input", ""), 600)
        intent = _short(state.get("current_intent", ""), 120) or "unknown"
        signature = compute_task_signature(intent, user_input)
        plan_dag = _compress_plan_dag(
            state.get("task_queue", []) or [],
            max_steps=int(getattr(settings, "EPISODE_REPLAY_MAX_DAG_STEPS", 8)),
        )

        cost_usd = float(state.get("total_cost_usd", 0.0) or 0.0)
        elapsed_ms = int(state.get("elapsed_ms", 0) or 0)
        llm_calls = int(state.get("llm_call_count", 0) or 0)

        row = (
            signature,
            user_input,
            intent,
            json.dumps(plan_dag, ensure_ascii=False),
            outcome,
            cost_usd,
            elapsed_ms,
            llm_calls,
            "",
            _short(state.get("job_id", ""), 120),
            _short(state.get("session_id", ""), 120),
            _now_iso(),
        )
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO episodes (task_signature, user_input, intent, plan_dag_json,"
                " outcome, cost_usd, elapsed_ms, llm_calls, lessons, job_id, session_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            return int(cur.lastrowid)

    def update_lessons(self, episode_id: int, lessons: str) -> None:
        text = _short(lessons, _LESSON_LIMIT)
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE episodes SET lessons = ? WHERE id = ?",
                (text, int(episode_id)),
            )

    # ------------------------------------------------------------------
    # read path
    # ------------------------------------------------------------------

    def search_similar(
        self,
        *,
        user_input: str,
        top_k: int = 2,
        max_age_days: int = 60,
        min_similarity: float = 0.45,
        require_outcome: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Token-overlap nearest-neighbor search.

        Returns rows ordered by similarity desc; each row dict contains
        decoded plan_dag and a `score` field. When `require_outcome` is set,
        only rows whose outcome matches are returned.
        """
        if top_k <= 0:
            return []
        cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat(timespec="seconds")
        sql = "SELECT * FROM episodes WHERE created_at >= ?"
        params: List[Any] = [cutoff]
        if require_outcome:
            sql += " AND outcome = ?"
            params.append(require_outcome)
        sql += " ORDER BY created_at DESC LIMIT 200"

        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        query_tokens = _tokenize(user_input)
        scored: List[tuple[float, Dict[str, Any]]] = []
        for row in rows:
            candidate_tokens = _tokenize(row["user_input"])
            score = _token_overlap_score(query_tokens, candidate_tokens)
            if score < min_similarity:
                continue
            scored.append((score, _row_to_dict(row, score)))

        scored.sort(key=lambda pair: (pair[0], pair[1]["created_at"]), reverse=True)
        return [item for _, item in scored[: max(top_k, 1)]]

    def fetch_brief_pair(
        self,
        *,
        user_input: str,
        max_age_days: int = 60,
        min_similarity: float = 0.45,
    ) -> List[Dict[str, Any]]:
        """便捷接口：返回 1 条 success + 1 条 fail（按相似度），最多 2 条。"""
        success = self.search_similar(
            user_input=user_input,
            top_k=1,
            max_age_days=max_age_days,
            min_similarity=min_similarity,
            require_outcome="success",
        )
        failure = self.search_similar(
            user_input=user_input,
            top_k=1,
            max_age_days=max_age_days,
            min_similarity=min_similarity,
            require_outcome="fail",
        )
        return success + failure

    # ------------------------------------------------------------------
    # housekeeping
    # ------------------------------------------------------------------

    def purge_older_than(self, *, max_age_days: int) -> int:
        cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM episodes WHERE created_at < ?", (cutoff,))
            return cur.rowcount or 0


def _row_to_dict(row: sqlite3.Row, score: float) -> Dict[str, Any]:
    try:
        plan_dag = json.loads(row["plan_dag_json"]) if row["plan_dag_json"] else []
    except (TypeError, ValueError, json.JSONDecodeError):
        plan_dag = []
    return {
        "id": row["id"],
        "task_signature": row["task_signature"],
        "user_input": row["user_input"],
        "intent": row["intent"],
        "plan_dag": plan_dag,
        "outcome": row["outcome"],
        "cost_usd": row["cost_usd"],
        "elapsed_ms": row["elapsed_ms"],
        "llm_calls": row["llm_calls"],
        "lessons": row["lessons"] or "",
        "job_id": row["job_id"] or "",
        "session_id": row["session_id"] or "",
        "created_at": row["created_at"],
        "score": round(float(score), 3),
    }


# ---------------------------------------------------------------------------
# Module-level singleton + prompt formatter
# ---------------------------------------------------------------------------

_store_lock = threading.Lock()
_store_instance: Optional[EpisodeStore] = None


def get_episode_store() -> EpisodeStore:
    global _store_instance
    with _store_lock:
        if _store_instance is None:
            _store_instance = EpisodeStore()
        return _store_instance


def reset_episode_store_singleton_for_tests() -> None:
    """Test helper: drop cached singleton so a new EPISODE_REPLAY_DB can take effect."""
    global _store_instance
    with _store_lock:
        _store_instance = None


def format_episode_brief(episodes: List[Dict[str, Any]]) -> str:
    """Render retrieved episodes as a compact markdown block for prompt injection.

    Empty input → empty string (caller can short-circuit).
    """
    if not episodes:
        return ""
    lines = [
        "## Past similar task traces (reference, do not blindly copy):",
    ]
    for item in episodes:
        outcome = item.get("outcome", "?")
        marker = "[success]" if outcome == "success" else (
            "[failure]" if outcome == "fail" else f"[{outcome}]"
        )
        cost = item.get("cost_usd") or 0.0
        elapsed_ms = item.get("elapsed_ms") or 0
        elapsed_s = elapsed_ms / 1000.0 if elapsed_ms else 0.0
        steps = item.get("plan_dag") or []
        header = f"- {marker} task=\"{_short(item.get('user_input', ''), 90)}\""
        meta_bits = []
        if steps:
            meta_bits.append(f"{len(steps)} step(s)")
        if elapsed_s:
            meta_bits.append(f"{elapsed_s:.1f}s")
        if cost:
            meta_bits.append(f"${cost:.4f}")
        if meta_bits:
            header += " | " + ", ".join(meta_bits)
        lines.append(header)
        for index, step in enumerate(steps, start=1):
            tool = step.get("tool", "")
            intent = step.get("intent", "")
            status = step.get("status", "")
            seg = f"    {index}) {tool}"
            if intent:
                seg += f" — {intent}"
            if status and status not in {"completed", "success"}:
                seg += f" [{status}]"
            lines.append(seg)
        lessons = (item.get("lessons") or "").strip()
        if lessons:
            lines.append(f"    lesson: {_short(lessons, 200)}")
    lines.append("")
    return "\n".join(lines) + "---\n"


__all__ = [
    "EpisodeStore",
    "compute_task_signature",
    "format_episode_brief",
    "get_episode_store",
    "reset_episode_store_singleton_for_tests",
]
