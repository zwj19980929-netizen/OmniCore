"""
Lightweight work context persistence for long-running personal agent workflows.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import settings


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _short(value: Any, limit: int = 500) -> str:
    return str(value or "")[:limit]


def _tokenize(text: str) -> set[str]:
    raw = str(text or "").lower()
    normalized = []
    cjk_runs: List[str] = []
    current_cjk: List[str] = []
    for ch in raw:
        if "\u4e00" <= ch <= "\u9fff":
            current_cjk.append(ch)
            normalized.append(" ")
            continue
        if current_cjk:
            cjk_runs.append("".join(current_cjk))
            current_cjk = []
        normalized.append(ch if ch.isalnum() else " ")
    if current_cjk:
        cjk_runs.append("".join(current_cjk))

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


def _recency_score(timestamp: str) -> float:
    try:
        delta = datetime.now() - datetime.fromisoformat(str(timestamp))
    except (TypeError, ValueError):
        return 0.0
    days = max(delta.total_seconds() / 86400.0, 0.0)
    return max(0.0, 1.0 - min(days / 30.0, 1.0))


def _token_overlap_score(query_tokens: set[str], candidate_tokens: set[str]) -> float:
    if not query_tokens:
        return 0.0
    overlap = len(query_tokens & candidate_tokens)
    if overlap <= 0:
        return 0.0
    recall = overlap / max(len(query_tokens), 1)
    precision = overlap / max(len(candidate_tokens), 1)
    return (recall * 0.75) + (precision * 0.25)


class WorkContextStore:
    def __init__(self, state_dir: Optional[Path] = None):
        self.state_dir = Path(state_dir) if state_dir else settings.DATA_DIR / "work_context"
        self.goals_path = self.state_dir / "goals.jsonl"
        self.projects_path = self.state_dir / "projects.jsonl"
        self.todos_path = self.state_dir / "todos.jsonl"
        self.experiences_path = self.state_dir / "experiences.jsonl"
        self._lock = threading.Lock()

    def _ensure_dir(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _read_jsonl_locked(self, path: Path) -> List[Dict[str, Any]]:
        if not path.exists():
            return []
        records: List[Dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    records.append(item)
        return records

    def _write_jsonl_locked(self, path: Path, records: List[Dict[str, Any]]) -> None:
        self._ensure_dir()
        with path.open("w", encoding="utf-8") as handle:
            for item in records:
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True))
                handle.write("\n")

    def create_goal(
        self,
        *,
        session_id: str,
        title: str,
        description: str = "",
    ) -> Dict[str, Any]:
        record = {
            "goal_id": _new_id("goal"),
            "session_id": _short(session_id, 120),
            "title": _short(title, 200),
            "description": _short(description, 1000),
            "status": "active",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "last_job_id": "",
        }
        with self._lock:
            records = self._read_jsonl_locked(self.goals_path)
            records.append(record)
            self._write_jsonl_locked(self.goals_path, records)
        return dict(record)

    def create_project(
        self,
        *,
        session_id: str,
        title: str,
        goal_id: str = "",
        description: str = "",
    ) -> Dict[str, Any]:
        record = {
            "project_id": _new_id("project"),
            "session_id": _short(session_id, 120),
            "goal_id": _short(goal_id, 120),
            "title": _short(title, 200),
            "description": _short(description, 1000),
            "status": "active",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "last_job_id": "",
        }
        with self._lock:
            records = self._read_jsonl_locked(self.projects_path)
            records.append(record)
            self._write_jsonl_locked(self.projects_path, records)
        return dict(record)

    def create_todo(
        self,
        *,
        session_id: str,
        title: str,
        goal_id: str = "",
        project_id: str = "",
        details: str = "",
    ) -> Dict[str, Any]:
        record = {
            "todo_id": _new_id("todo"),
            "session_id": _short(session_id, 120),
            "goal_id": _short(goal_id, 120),
            "project_id": _short(project_id, 120),
            "title": _short(title, 200),
            "details": _short(details, 1000),
            "status": "pending",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "last_job_id": "",
        }
        with self._lock:
            records = self._read_jsonl_locked(self.todos_path)
            records.append(record)
            self._write_jsonl_locked(self.todos_path, records)
        return dict(record)

    def list_goals(self, *, session_id: Optional[str] = None, limit: Optional[int] = 100) -> List[Dict[str, Any]]:
        with self._lock:
            records = self._read_jsonl_locked(self.goals_path)
        if session_id:
            records = [item for item in records if str(item.get("session_id", "")) == str(session_id)]
        if limit is None:
            return records
        return records[-max(limit, 0):]

    def list_projects(
        self,
        *,
        session_id: Optional[str] = None,
        goal_id: Optional[str] = None,
        limit: Optional[int] = 100,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            records = self._read_jsonl_locked(self.projects_path)
        if session_id:
            records = [item for item in records if str(item.get("session_id", "")) == str(session_id)]
        if goal_id:
            records = [item for item in records if str(item.get("goal_id", "")) == str(goal_id)]
        if limit is None:
            return records
        return records[-max(limit, 0):]

    def list_todos(
        self,
        *,
        session_id: Optional[str] = None,
        goal_id: Optional[str] = None,
        project_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = 200,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            records = self._read_jsonl_locked(self.todos_path)
        if session_id:
            records = [item for item in records if str(item.get("session_id", "")) == str(session_id)]
        if goal_id:
            records = [item for item in records if str(item.get("goal_id", "")) == str(goal_id)]
        if project_id:
            records = [item for item in records if str(item.get("project_id", "")) == str(project_id)]
        if status:
            records = [item for item in records if str(item.get("status", "")) == str(status)]
        if limit is None:
            return records
        return records[-max(limit, 0):]

    def update_todo_status(self, todo_id: str, status: str, *, last_job_id: str = "") -> Dict[str, Any]:
        with self._lock:
            records = self._read_jsonl_locked(self.todos_path)
            updated: Dict[str, Any] = {}
            for item in records:
                if str(item.get("todo_id", "")) != str(todo_id):
                    continue
                item["status"] = _short(status, 40)
                item["updated_at"] = _now_iso()
                if last_job_id:
                    item["last_job_id"] = _short(last_job_id, 120)
                updated = dict(item)
                break
            self._write_jsonl_locked(self.todos_path, records)
        return updated

    def record_job_link(
        self,
        *,
        job_id: str,
        goal_id: str = "",
        project_id: str = "",
        todo_id: str = "",
        success: bool = False,
    ) -> None:
        now = _now_iso()
        with self._lock:
            if goal_id:
                goals = self._read_jsonl_locked(self.goals_path)
                for item in goals:
                    if str(item.get("goal_id", "")) == str(goal_id):
                        item["last_job_id"] = _short(job_id, 120)
                        item["updated_at"] = now
                        if success:
                            item["status"] = item.get("status", "active") or "active"
                        break
                self._write_jsonl_locked(self.goals_path, goals)
            if project_id:
                projects = self._read_jsonl_locked(self.projects_path)
                for item in projects:
                    if str(item.get("project_id", "")) == str(project_id):
                        item["last_job_id"] = _short(job_id, 120)
                        item["updated_at"] = now
                        break
                self._write_jsonl_locked(self.projects_path, projects)
            if todo_id:
                todos = self._read_jsonl_locked(self.todos_path)
                for item in todos:
                    if str(item.get("todo_id", "")) == str(todo_id):
                        item["last_job_id"] = _short(job_id, 120)
                        item["updated_at"] = now
                        item["status"] = "done" if success else "in_progress"
                        break
                self._write_jsonl_locked(self.todos_path, todos)

    def get_context_snapshot(
        self,
        *,
        session_id: str,
        goal_id: str = "",
        project_id: str = "",
        todo_id: str = "",
    ) -> Dict[str, Any]:
        goals = self.list_goals(session_id=session_id, limit=None)
        projects = self.list_projects(session_id=session_id, limit=None)
        todos = self.list_todos(session_id=session_id, limit=None)

        selected_goal = next((item for item in goals if str(item.get("goal_id", "")) == str(goal_id)), {})
        selected_project = next((item for item in projects if str(item.get("project_id", "")) == str(project_id)), {})
        selected_todo = next((item for item in todos if str(item.get("todo_id", "")) == str(todo_id)), {})

        related_todos = []
        for item in todos:
            if goal_id and str(item.get("goal_id", "")) != str(goal_id):
                continue
            if project_id and str(item.get("project_id", "")) != str(project_id):
                continue
            related_todos.append(dict(item))

        return {
            "goal": selected_goal,
            "project": selected_project,
            "todo": selected_todo,
            "open_todos": [
                item for item in related_todos
                if str(item.get("status", "")) not in {"done", "cancelled"}
            ][:10],
        }

    def record_experience(
        self,
        *,
        session_id: str,
        job_id: str,
        user_input: str,
        intent: str,
        tool_sequence: List[str],
        success: bool,
        goal_id: str = "",
        project_id: str = "",
        todo_id: str = "",
        summary: str = "",
        task_details: Optional[List[Dict[str, Any]]] = None,
        visited_urls: Optional[List[str]] = None,
        artifact_refs: Optional[List[Dict[str, Any]]] = None,
        failure_reason: str = "",
    ) -> Dict[str, Any]:
        record = {
            "experience_id": _new_id("xp"),
            "session_id": _short(session_id, 120),
            "job_id": _short(job_id, 120),
            "goal_id": _short(goal_id, 120),
            "project_id": _short(project_id, 120),
            "todo_id": _short(todo_id, 120),
            "created_at": _now_iso(),
            "user_input": _short(user_input, 300),
            "intent": _short(intent, 120),
            "tool_sequence": [_short(item, 120) for item in (tool_sequence or []) if str(item).strip()],
            "success": bool(success),
            "summary": _short(summary, 300),
            "failure_reason": _short(failure_reason, 300),
            "visited_urls": [
                _short(item, 240)
                for item in (visited_urls or [])
                if str(item).strip()
            ][:10],
            "artifact_refs": [
                {
                    "artifact_type": _short((item or {}).get("artifact_type", ""), 60),
                    "name": _short((item or {}).get("name", ""), 180),
                    "path": _short((item or {}).get("path", ""), 300),
                }
                for item in (artifact_refs or [])
                if isinstance(item, dict)
            ][:8],
            "task_details": [
                {
                    "tool_name": _short((item or {}).get("tool_name", ""), 120),
                    "task_type": _short((item or {}).get("task_type", ""), 120),
                    "status": _short((item or {}).get("status", ""), 40),
                    "description": _short((item or {}).get("description", ""), 240),
                }
                for item in (task_details or [])
                if isinstance(item, dict)
            ][:12],
        }
        with self._lock:
            records = self._read_jsonl_locked(self.experiences_path)
            records.append(record)
            if len(records) > 1000:
                records = records[-1000:]
            self._write_jsonl_locked(self.experiences_path, records)
        return dict(record)

    def _score_experience(
        self,
        item: Dict[str, Any],
        *,
        query_tokens: set[str],
        session_id: str = "",
        goal_id: str = "",
    ) -> float:
        text = " ".join(
            [
                str(item.get("user_input", "") or ""),
                str(item.get("intent", "") or ""),
                " ".join(item.get("tool_sequence", []) or []),
                str(item.get("summary", "") or ""),
                str(item.get("failure_reason", "") or ""),
                " ".join(item.get("visited_urls", []) or []),
                " ".join(
                    str(detail.get("description", "") or "")
                    for detail in (item.get("task_details", []) or [])
                    if isinstance(detail, dict)
                ),
            ]
        )
        candidate_tokens = _tokenize(text)
        lexical = _token_overlap_score(query_tokens, candidate_tokens)
        if lexical <= 0 and query_tokens:
            return 0.0

        scope_boost = 0.0
        if session_id and str(item.get("session_id", "")) == str(session_id):
            scope_boost += 1.0
        elif str(item.get("session_id", "")) == "":
            scope_boost += 0.2

        if goal_id and str(item.get("goal_id", "")) == str(goal_id):
            scope_boost += 0.6
        elif goal_id and str(item.get("goal_id", "")) not in {"", str(goal_id)}:
            return 0.0

        return (lexical * 10.0) + scope_boost + _recency_score(str(item.get("created_at", "") or ""))

    def suggest_success_paths(
        self,
        *,
        query: str,
        session_id: str = "",
        goal_id: str = "",
        limit: int = 3,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            records = self._read_jsonl_locked(self.experiences_path)

        query_tokens = _tokenize(query)
        scored = []
        for item in records:
            if not bool(item.get("success", False)):
                continue
            score = self._score_experience(
                item,
                query_tokens=query_tokens,
                session_id=session_id,
                goal_id=goal_id,
            )
            if score <= 0:
                continue
            scored.append((score, item))

        scored.sort(key=lambda pair: (pair[0], pair[1].get("created_at", "")), reverse=True)
        suggestions: List[Dict[str, Any]] = []
        for score, item in scored[: max(limit, 1)]:
            candidate = dict(item)
            candidate["match_score"] = round(score, 3)
            suggestions.append(candidate)
        return suggestions

    def suggest_failure_avoidance(
        self,
        *,
        query: str,
        session_id: str = "",
        goal_id: str = "",
        limit: int = 3,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            records = self._read_jsonl_locked(self.experiences_path)

        query_tokens = _tokenize(query)
        scored = []
        for item in records:
            if bool(item.get("success", False)):
                continue
            score = self._score_experience(
                item,
                query_tokens=query_tokens,
                session_id=session_id,
                goal_id=goal_id,
            )
            if score <= 0:
                continue
            scored.append((score, item))

        scored.sort(key=lambda pair: (pair[0], pair[1].get("created_at", "")), reverse=True)
        suggestions: List[Dict[str, Any]] = []
        for score, item in scored[: max(limit, 1)]:
            candidate = dict(item)
            candidate["match_score"] = round(score, 3)
            suggestions.append(candidate)
        return suggestions


_work_context_store: Optional[WorkContextStore] = None


def get_work_context_store() -> WorkContextStore:
    global _work_context_store
    if _work_context_store is None:
        _work_context_store = WorkContextStore()
    return _work_context_store
