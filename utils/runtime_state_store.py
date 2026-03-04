"""
Persistence helpers for session/job/artifact runtime state.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import settings


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _short_text(value: Any, limit: int = 300) -> str:
    text = str(value or "")
    return text[:limit]


def _parse_daily_time(value: Any, *, now: Optional[datetime] = None) -> datetime:
    current = now or datetime.now()
    raw = str(value or "").strip()
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour = min(max(int(hour_text), 0), 23)
        minute = min(max(int(minute_text), 0), 59)
    except (TypeError, ValueError):
        hour = current.hour
        minute = current.minute

    candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= current:
        candidate += timedelta(days=1)
    return candidate


def _compute_next_schedule_run(record: Dict[str, Any], *, from_time: Optional[datetime] = None) -> str:
    current = from_time or datetime.now()
    schedule_type = str(record.get("schedule_type", "") or "").strip().lower()
    if schedule_type == "once":
        return ""
    if schedule_type == "interval":
        interval_seconds = max(int(record.get("interval_seconds", 0) or 0), 60)
        return (current + timedelta(seconds=interval_seconds)).isoformat(timespec="seconds")
    if schedule_type == "daily":
        return _parse_daily_time(record.get("time_of_day"), now=current).isoformat(timespec="seconds")
    return ""


def _artifact_type_for_path(path_value: str, task_type: str, source_key: str) -> str:
    suffix = Path(path_value).suffix.lower()
    if source_key == "screenshot_path" or suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        return "image"
    if source_key == "download_path":
        return "download"
    if task_type == "file_worker":
        return "file"
    return "artifact"


def _artifact_fingerprint(record: Dict[str, Any]) -> str:
    if record.get("path"):
        return f"path:{record.get('path')}"
    if record.get("source_key") and record.get("preview"):
        return f"inline:{record.get('source_key')}:{record.get('preview')}"
    if record.get("name"):
        return f"name:{record.get('name')}"
    return f"artifact:{record.get('artifact_id', '')}"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
        }

    content = getattr(value, "content", None)
    if content is not None:
        return {
            "type": value.__class__.__name__,
            "content": _short_text(content, 1000),
        }

    return _short_text(repr(value), 1000)


def _checkpoint_snapshot(state: Dict[str, Any]) -> Dict[str, Any]:
    payload = state if isinstance(state, dict) else {}
    keys = [
        "user_input",
        "session_id",
        "job_id",
        "current_intent",
        "execution_status",
        "final_output",
        "error_trace",
        "replan_count",
        "needs_human_confirm",
        "human_approved",
        "critic_approved",
        "critic_feedback",
        "validator_passed",
        "task_queue",
        "shared_memory",
        "policy_decisions",
        "artifacts",
        "delivery_package",
    ]
    return {
        key: _json_safe(payload.get(key))
        for key in keys
        if key in payload
    }


class RuntimeStateStore:
    def __init__(self, state_dir: Optional[Path] = None):
        self.state_dir = Path(state_dir) if state_dir else settings.DATA_DIR / "runtime_state"
        self.sessions_path = self.state_dir / "sessions.json"
        self.jobs_path = self.state_dir / "jobs.jsonl"
        self.artifacts_path = self.state_dir / "artifacts.jsonl"
        self.checkpoints_path = self.state_dir / "checkpoints.jsonl"
        self.queue_path = self.state_dir / "job_queue.jsonl"
        self.worker_path = self.state_dir / "worker.json"
        self.schedules_path = self.state_dir / "schedules.jsonl"
        self.notifications_path = self.state_dir / "notifications.jsonl"
        self.preferences_path = self.state_dir / "preferences.json"
        self._lock = threading.Lock()

    def _ensure_parent_dir(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _read_sessions_locked(self) -> Dict[str, Dict[str, Any]]:
        if not self.sessions_path.exists():
            return {}
        try:
            payload = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            str(key): value
            for key, value in payload.items()
            if isinstance(value, dict)
        }

    def _write_sessions_locked(self, sessions: Dict[str, Dict[str, Any]]) -> None:
        self._ensure_parent_dir()
        self.sessions_path.write_text(
            json.dumps(sessions, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _read_jsonl_locked(self, path: Path) -> List[Dict[str, Any]]:
        if not path.exists():
            return []
        records: List[Dict[str, Any]] = []
        try:
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
        except FileNotFoundError:
            return []
        return records

    def _write_jsonl_locked(self, path: Path, records: List[Dict[str, Any]]) -> None:
        self._ensure_parent_dir()
        with path.open("w", encoding="utf-8") as handle:
            for item in records:
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True))
                handle.write("\n")

    def _read_worker_locked(self) -> Dict[str, Any]:
        if not self.worker_path.exists():
            return {}
        try:
            payload = json.loads(self.worker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_worker_locked(self, record: Dict[str, Any]) -> None:
        self._ensure_parent_dir()
        self.worker_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _read_json_locked(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_json_locked(self, path: Path, record: Dict[str, Any]) -> None:
        self._ensure_parent_dir()
        path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _submit_job_locked(
        self,
        *,
        session_id: str,
        user_input: str,
        is_special_command: bool = False,
        trigger_source: str = "manual",
        schedule_id: str = "",
        goal_id: str = "",
        project_id: str = "",
        todo_id: str = "",
    ) -> Dict[str, Any]:
        sessions = self._read_sessions_locked()
        session = dict(sessions.get(session_id) or {
            "session_id": session_id,
            "created_at": _now_iso(),
            "job_count": 0,
            "last_job_id": "",
            "last_user_input": "",
            "source": "runtime",
            "status": "active",
        })
        now = _now_iso()
        job_record = {
            "job_id": _new_id("job"),
            "session_id": session_id,
            "created_at": now,
            "updated_at": now,
            "status": "queued",
            "success": None,
            "user_input": _short_text(user_input, 500),
            "is_special_command": bool(is_special_command),
            "artifact_ids": [],
            "task_count": 0,
            "tasks_completed": 0,
            "intent": "",
            "error": "",
            "output_preview": "",
            "policy_decisions": [],
            "checkpoint_count": 0,
            "last_checkpoint_id": "",
            "last_checkpoint_stage": "",
            "last_checkpoint_at": "",
            "trigger_source": trigger_source,
            "schedule_id": schedule_id,
            "goal_id": _short_text(goal_id, 120),
            "project_id": _short_text(project_id, 120),
            "todo_id": _short_text(todo_id, 120),
        }

        jobs = self._read_jsonl_locked(self.jobs_path)
        jobs.append(job_record)
        self._write_jsonl_locked(self.jobs_path, jobs)

        queue_records = self._read_jsonl_locked(self.queue_path)
        queue_records.append({
            "queue_id": _new_id("queue"),
            "job_id": job_record["job_id"],
            "session_id": session_id,
            "created_at": now,
            "updated_at": now,
            "status": "queued",
            "user_input": job_record["user_input"],
            "is_special_command": bool(is_special_command),
                "trigger_source": trigger_source,
                "schedule_id": schedule_id,
                "goal_id": _short_text(goal_id, 120),
                "project_id": _short_text(project_id, 120),
                "todo_id": _short_text(todo_id, 120),
            })
        self._write_jsonl_locked(self.queue_path, queue_records)

        session["updated_at"] = now
        session["last_job_id"] = job_record["job_id"]
        session["last_user_input"] = _short_text(user_input, 160)
        session["job_count"] = int(session.get("job_count", 0) or 0) + 1
        session["status"] = "active"
        sessions[session_id] = session
        self._write_sessions_locked(sessions)

        return dict(job_record)

    def get_or_create_session(
        self,
        *,
        session_id: Optional[str] = None,
        source: str = "runtime",
    ) -> Dict[str, Any]:
        with self._lock:
            sessions = self._read_sessions_locked()
            resolved_id = str(session_id or "").strip() or _new_id("session")
            now = _now_iso()
            record = dict(sessions.get(resolved_id, {}))
            if not record:
                record = {
                    "session_id": resolved_id,
                    "created_at": now,
                    "job_count": 0,
                    "last_job_id": "",
                    "last_user_input": "",
                    "source": source,
                    "status": "active",
                }
            record["updated_at"] = now
            record["source"] = source or record.get("source", "runtime")
            sessions[resolved_id] = record
            self._write_sessions_locked(sessions)
            return dict(record)

    def update_worker_state(
        self,
        *,
        status: str,
        worker_id: str = "",
        last_job_id: str = "",
        note: str = "",
        mode: str = "",
        pid: int = 0,
    ) -> Dict[str, Any]:
        with self._lock:
            record = self._read_worker_locked()
            if not record:
                record = {
                    "worker_id": worker_id or "",
                    "started_at": _now_iso(),
                }
            if worker_id:
                record["worker_id"] = worker_id
            if not record.get("started_at"):
                record["started_at"] = _now_iso()
            record["status"] = status
            record["last_job_id"] = last_job_id or record.get("last_job_id", "")
            record["note"] = note
            if mode:
                record["mode"] = mode
            elif "mode" not in record:
                record["mode"] = "thread"
            if pid:
                record["pid"] = int(pid)
            elif status == "stopped":
                record["pid"] = 0
            record["heartbeat_at"] = _now_iso()
            record["updated_at"] = record["heartbeat_at"]
            self._write_worker_locked(record)
            return dict(record)

    def get_worker_state(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._read_worker_locked())

    def submit_job(
        self,
        *,
        session_id: str,
        user_input: str,
        is_special_command: bool = False,
        trigger_source: str = "manual",
        schedule_id: str = "",
        goal_id: str = "",
        project_id: str = "",
        todo_id: str = "",
    ) -> Dict[str, Any]:
        with self._lock:
            return self._submit_job_locked(
                session_id=session_id,
                user_input=user_input,
                is_special_command=is_special_command,
                trigger_source=trigger_source,
                schedule_id=schedule_id,
                goal_id=goal_id,
                project_id=project_id,
                todo_id=todo_id,
            )

    def start_job(
        self,
        *,
        job_id: Optional[str] = None,
        session_id: str,
        user_input: str,
        is_special_command: bool = False,
    ) -> Dict[str, Any]:
        resolved_job_id = str(job_id or "").strip()
        if not resolved_job_id:
            submitted = self.submit_job(
                session_id=session_id,
                user_input=user_input,
                is_special_command=is_special_command,
            )
            resolved_job_id = str(submitted.get("job_id", "") or "")

        with self._lock:
            jobs = self._read_jsonl_locked(self.jobs_path)
            now = _now_iso()
            job_record: Dict[str, Any] = {}
            for item in jobs:
                if str(item.get("job_id", "")) != resolved_job_id:
                    continue
                item["updated_at"] = now
                item["status"] = "running"
                item["user_input"] = _short_text(user_input, 500)
                item["is_special_command"] = bool(is_special_command)
                job_record = dict(item)
                break
            self._write_jsonl_locked(self.jobs_path, jobs)

            queue_records = self._read_jsonl_locked(self.queue_path)
            for item in queue_records:
                if str(item.get("job_id", "")) != resolved_job_id:
                    continue
                item["updated_at"] = now
                item["status"] = "running"
                break
            if queue_records:
                self._write_jsonl_locked(self.queue_path, queue_records)

            return job_record

    def register_task_artifacts(
        self,
        *,
        session_id: str,
        job_id: str,
        tasks: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if not session_id or not job_id:
            return []

        source_keys = ["file_path", "path", "output_path", "download_path", "screenshot_path"]
        inline_keys = ["data", "items", "content"]
        registered: List[Dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        with self._lock:
            existing = self._read_jsonl_locked(self.artifacts_path)
            existing_keys = {
                (str(item.get("job_id", "")), _artifact_fingerprint(item))
                for item in existing
                if isinstance(item, dict)
            }

            for task in tasks or []:
                if not isinstance(task, dict):
                    continue
                if task.get("status") != "completed":
                    continue

                task_id = str(task.get("task_id", "") or "")
                task_type = str(task.get("task_type", "") or "")
                result = task.get("result", {})
                params = task.get("params", {})
                result_dict = result if isinstance(result, dict) else {}
                params_dict = params if isinstance(params, dict) else {}

                candidates: List[tuple[str, str]] = []
                for key in source_keys:
                    value = result_dict.get(key)
                    if isinstance(value, str) and value.strip():
                        candidates.append((key, value.strip()))

                if task_type == "file_worker" and params_dict.get("action") == "write":
                    path_value = params_dict.get("file_path")
                    if isinstance(path_value, str) and path_value.strip():
                        candidates.append(("file_path", path_value.strip()))

                for source_key, path_value in candidates:
                    dedupe_key = (task_id, path_value)
                    prospective = {
                        "artifact_id": "",
                        "path": path_value,
                    }
                    fingerprint = _artifact_fingerprint(prospective)
                    if dedupe_key in seen or (job_id, fingerprint) in existing_keys:
                        continue
                    seen.add(dedupe_key)

                    artifact_record = {
                        "artifact_id": _new_id("artifact"),
                        "session_id": session_id,
                        "job_id": job_id,
                        "task_id": task_id,
                        "created_at": _now_iso(),
                        "artifact_type": _artifact_type_for_path(path_value, task_type, source_key),
                        "source_key": source_key,
                        "path": path_value,
                        "name": Path(path_value).name or path_value,
                        "task_type": task_type,
                        "tool_name": str(task.get("tool_name", "") or ""),
                    }
                    registered.append(artifact_record)
                    existing.append(artifact_record)
                    existing_keys.add((job_id, _artifact_fingerprint(artifact_record)))

                for source_key in inline_keys:
                    value = result_dict.get(source_key)
                    if value in (None, "", [], {}):
                        continue
                    preview = _short_text(
                        json.dumps(value, ensure_ascii=False)
                        if isinstance(value, (dict, list))
                        else value,
                        240,
                    )
                    artifact_record = {
                        "artifact_id": _new_id("artifact"),
                        "session_id": session_id,
                        "job_id": job_id,
                        "task_id": task_id,
                        "created_at": _now_iso(),
                        "artifact_type": "structured_data",
                        "source_key": source_key,
                        "path": "",
                        "name": f"{task_id}_{source_key}",
                        "task_type": task_type,
                        "tool_name": str(task.get("tool_name", "") or ""),
                        "preview": preview,
                    }
                    fingerprint = _artifact_fingerprint(artifact_record)
                    if (job_id, fingerprint) in existing_keys:
                        continue
                    registered.append(artifact_record)
                    existing.append(artifact_record)
                    existing_keys.add((job_id, fingerprint))

            if registered:
                self._write_jsonl_locked(self.artifacts_path, existing)

        return registered

    def complete_job(
        self,
        *,
        session_id: str,
        job_id: str,
        status: str,
        success: bool,
        output: str,
        error: str,
        intent: str,
        tasks: List[Dict[str, Any]],
        policy_decisions: List[Dict[str, Any]],
        artifacts: List[Dict[str, Any]],
        is_special_command: bool = False,
    ) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            jobs = self._read_jsonl_locked(self.jobs_path)
            sessions = self._read_sessions_locked()
            updated_job: Dict[str, Any] = {}
            now = _now_iso()

            for item in jobs:
                if str(item.get("job_id", "")) != job_id:
                    continue
                item["updated_at"] = now
                item["status"] = status or item.get("status", "")
                item["success"] = bool(success)
                item["intent"] = _short_text(intent, 120)
                item["error"] = _short_text(error, 500)
                item["output_preview"] = _short_text(output, 500)
                item["artifact_ids"] = [a.get("artifact_id", "") for a in artifacts if isinstance(a, dict)]
                item["policy_decisions"] = [
                    dict(decision)
                    for decision in policy_decisions or []
                    if isinstance(decision, dict)
                ]
                item["task_count"] = len(tasks or [])
                item["tasks_completed"] = sum(
                    1 for task in tasks or []
                    if isinstance(task, dict) and task.get("status") == "completed"
                )
                item["is_special_command"] = bool(is_special_command)
                item["goal_id"] = _short_text(item.get("goal_id", ""), 120)
                item["project_id"] = _short_text(item.get("project_id", ""), 120)
                item["todo_id"] = _short_text(item.get("todo_id", ""), 120)
                updated_job = dict(item)
                break

            self._write_jsonl_locked(self.jobs_path, jobs)

            queue_records = self._read_jsonl_locked(self.queue_path)
            for item in queue_records:
                if str(item.get("job_id", "")) != job_id:
                    continue
                item["updated_at"] = now
                item["status"] = status or item.get("status", "")
                break
            if queue_records:
                self._write_jsonl_locked(self.queue_path, queue_records)

            session = dict(sessions.get(session_id) or {
                "session_id": session_id,
                "created_at": now,
                "job_count": 0,
                "last_job_id": job_id,
                "last_user_input": "",
                "source": "runtime",
            })
            session["updated_at"] = now
            session["last_job_id"] = job_id
            session["status"] = "active"
            sessions[session_id] = session
            self._write_sessions_locked(sessions)

            return {
                "job_record": updated_job,
                "session_record": dict(session),
            }

    def save_checkpoint(
        self,
        *,
        session_id: str,
        job_id: str,
        stage: str,
        state: Dict[str, Any],
        note: str = "",
    ) -> Dict[str, Any]:
        with self._lock:
            checkpoints = self._read_jsonl_locked(self.checkpoints_path)
            now = _now_iso()
            checkpoint = {
                "checkpoint_id": _new_id("checkpoint"),
                "session_id": session_id,
                "job_id": job_id,
                "created_at": now,
                "stage": _short_text(stage, 80),
                "note": _short_text(note, 300),
                "state": _checkpoint_snapshot(state),
            }
            checkpoints.append(checkpoint)
            self._write_jsonl_locked(self.checkpoints_path, checkpoints)

            jobs = self._read_jsonl_locked(self.jobs_path)
            for item in jobs:
                if str(item.get("job_id", "")) != str(job_id):
                    continue
                item["updated_at"] = now
                item["checkpoint_count"] = int(item.get("checkpoint_count", 0) or 0) + 1
                item["last_checkpoint_id"] = checkpoint["checkpoint_id"]
                item["last_checkpoint_stage"] = checkpoint["stage"]
                item["last_checkpoint_at"] = now
                break
            if jobs:
                self._write_jsonl_locked(self.jobs_path, jobs)

            return dict(checkpoint)

    def load_checkpoints(
        self,
        *,
        session_id: Optional[str] = None,
        job_id: Optional[str] = None,
        limit: Optional[int] = 20,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            checkpoints = self._read_jsonl_locked(self.checkpoints_path)
        if session_id:
            checkpoints = [item for item in checkpoints if str(item.get("session_id", "")) == str(session_id)]
        if job_id:
            checkpoints = [item for item in checkpoints if str(item.get("job_id", "")) == str(job_id)]
        if limit is None:
            return checkpoints
        return checkpoints[-max(limit, 0):]

    def get_latest_checkpoint(self, job_id: str) -> Dict[str, Any]:
        checkpoints = self.load_checkpoints(job_id=job_id, limit=None)
        if not checkpoints:
            return {}
        return dict(checkpoints[-1])

    def load_recent_jobs(self, limit: Optional[int] = 20) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = self._read_jsonl_locked(self.jobs_path)
        if limit is None:
            return jobs
        return jobs[-max(limit, 0):]

    def load_sessions(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._lock:
            sessions = list(self._read_sessions_locked().values())
        sessions.sort(key=lambda item: str(item.get("updated_at", "")))
        if limit is None:
            return sessions
        return sessions[-max(limit, 0):]

    def get_session(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._read_sessions_locked().get(str(session_id), {}))

    def load_jobs(
        self,
        *,
        session_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = 50,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = self._read_jsonl_locked(self.jobs_path)
        if session_id:
            jobs = [item for item in jobs if str(item.get("session_id", "")) == str(session_id)]
        if status:
            jobs = [item for item in jobs if str(item.get("status", "")) == str(status)]
        if limit is None:
            return jobs
        return jobs[-max(limit, 0):]

    def get_job(self, job_id: str) -> Dict[str, Any]:
        with self._lock:
            jobs = self._read_jsonl_locked(self.jobs_path)
        for item in jobs:
            if str(item.get("job_id", "")) == str(job_id):
                return dict(item)
        return {}

    def load_artifacts(
        self,
        *,
        session_id: Optional[str] = None,
        job_id: Optional[str] = None,
        limit: Optional[int] = 100,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            artifacts = self._read_jsonl_locked(self.artifacts_path)
        if session_id:
            artifacts = [item for item in artifacts if str(item.get("session_id", "")) == str(session_id)]
        if job_id:
            artifacts = [item for item in artifacts if str(item.get("job_id", "")) == str(job_id)]
        if limit is None:
            return artifacts
        return artifacts[-max(limit, 0):]

    def load_queue(self, limit: Optional[int] = 50) -> List[Dict[str, Any]]:
        with self._lock:
            queue_records = self._read_jsonl_locked(self.queue_path)
        if limit is None:
            return queue_records
        return queue_records[-max(limit, 0):]

    def claim_next_queued_job(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            queue_records = self._read_jsonl_locked(self.queue_path)
            now = _now_iso()
            claimed: Optional[Dict[str, Any]] = None
            for item in queue_records:
                if str(item.get("status", "")) != "queued":
                    continue
                item["status"] = "running"
                item["updated_at"] = now
                claimed = dict(item)
                break
            if claimed:
                self._write_jsonl_locked(self.queue_path, queue_records)
            return claimed

    def get_queue_summary(self) -> Dict[str, int]:
        queue_records = self.load_queue(limit=None)
        summary = {
            "queued": 0,
            "running": 0,
            "completed": 0,
            "error": 0,
            "cancelled": 0,
            "waiting_for_approval": 0,
            "waiting_for_event": 0,
            "blocked": 0,
        }
        for item in queue_records:
            status = str(item.get("status", "") or "")
            if status in summary:
                summary[status] += 1
        return summary

    def set_job_status(
        self,
        *,
        job_id: str,
        status: str,
        error: str = "",
    ) -> Dict[str, Any]:
        with self._lock:
            jobs = self._read_jsonl_locked(self.jobs_path)
            queue_records = self._read_jsonl_locked(self.queue_path)
            updated: Dict[str, Any] = {}
            now = _now_iso()
            for item in jobs:
                if str(item.get("job_id", "")) != str(job_id):
                    continue
                item["status"] = status
                item["updated_at"] = now
                if error:
                    item["error"] = _short_text(error, 500)
                updated = dict(item)
                break
            for item in queue_records:
                if str(item.get("job_id", "")) != str(job_id):
                    continue
                item["status"] = status
                item["updated_at"] = now
                break
            if jobs:
                self._write_jsonl_locked(self.jobs_path, jobs)
            if queue_records:
                self._write_jsonl_locked(self.queue_path, queue_records)
        return updated

    def recover_stale_running_jobs(self, stale_after_seconds: int) -> Dict[str, int]:
        cutoff_seconds = max(int(stale_after_seconds), 1)
        now = datetime.now()
        recovered_jobs = 0
        recovered_queue_items = 0

        with self._lock:
            jobs = self._read_jsonl_locked(self.jobs_path)
            queue_records = self._read_jsonl_locked(self.queue_path)
            worker = self._read_worker_locked()

            worker_stale = False
            heartbeat = _parse_iso(worker.get("heartbeat_at"))
            if worker and str(worker.get("status", "")) == "running" and heartbeat is not None:
                worker_stale = (now - heartbeat).total_seconds() > cutoff_seconds

            if not worker_stale:
                for item in queue_records:
                    if str(item.get("status", "")) != "running":
                        continue
                    updated_at = _parse_iso(item.get("updated_at"))
                    if updated_at is None:
                        continue
                    if (now - updated_at).total_seconds() > cutoff_seconds:
                        worker_stale = True
                        break

            if not worker_stale:
                return {
                    "jobs_requeued": 0,
                    "queue_items_requeued": 0,
                }

            for item in queue_records:
                if str(item.get("status", "")) != "running":
                    continue
                item["status"] = "queued"
                item["updated_at"] = _now_iso()
                recovered_queue_items += 1

            for item in jobs:
                if str(item.get("status", "")) != "running":
                    continue
                item["status"] = "queued"
                item["updated_at"] = _now_iso()
                recovered_jobs += 1

            if recovered_queue_items:
                self._write_jsonl_locked(self.queue_path, queue_records)
            if recovered_jobs:
                self._write_jsonl_locked(self.jobs_path, jobs)

            if worker:
                worker["status"] = "recovered"
                worker["note"] = f"Requeued {recovered_jobs} stale job(s)"
                worker["heartbeat_at"] = _now_iso()
                self._write_worker_locked(worker)

        return {
            "jobs_requeued": recovered_jobs,
            "queue_items_requeued": recovered_queue_items,
        }

    def get_preferences(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            record = self._read_json_locked(self.preferences_path)

        global_preferences = dict(record.get("global", {})) if isinstance(record, dict) else {}
        sessions = record.get("sessions", {}) if isinstance(record, dict) else {}
        session_preferences = {}
        if session_id and isinstance(sessions, dict):
            session_preferences = dict(sessions.get(str(session_id), {}))

        merged = dict(global_preferences)
        merged.update(session_preferences)
        if not merged:
            merged = {
                "default_output_directory": settings.DEFAULT_OUTPUT_DIRECTORY,
                "preferred_tools": list(settings.DEFAULT_PREFERRED_TOOLS),
                "preferred_sites": list(settings.DEFAULT_PREFERRED_SITES),
                "auto_queue_confirmations": bool(settings.DEFAULT_AUTO_QUEUE_CONFIRMATIONS),
                "task_templates": {},
            }

        merged.setdefault("default_output_directory", settings.DEFAULT_OUTPUT_DIRECTORY)
        merged.setdefault("preferred_tools", list(settings.DEFAULT_PREFERRED_TOOLS))
        merged.setdefault("preferred_sites", list(settings.DEFAULT_PREFERRED_SITES))
        merged.setdefault("auto_queue_confirmations", bool(settings.DEFAULT_AUTO_QUEUE_CONFIRMATIONS))
        merged.setdefault("task_templates", {})
        return merged

    def update_preferences(
        self,
        *,
        preferences: Dict[str, Any],
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        allowed = {
            "default_output_directory",
            "preferred_tools",
            "preferred_sites",
            "auto_queue_confirmations",
            "task_templates",
        }
        cleaned: Dict[str, Any] = {}
        for key, value in (preferences or {}).items():
            if key not in allowed:
                continue
            if key in {"preferred_tools", "preferred_sites"}:
                cleaned[key] = [
                    str(item).strip()
                    for item in (value or [])
                    if str(item).strip()
                ]
            elif key == "task_templates" and isinstance(value, dict):
                cleaned[key] = {
                    str(name).strip(): _short_text(template, 500)
                    for name, template in value.items()
                    if str(name).strip()
                }
            elif key == "auto_queue_confirmations":
                cleaned[key] = bool(value)
            else:
                cleaned[key] = _short_text(value, 300)

        with self._lock:
            payload = self._read_json_locked(self.preferences_path)
            if not payload:
                payload = {"global": {}, "sessions": {}}
            payload.setdefault("global", {})
            payload.setdefault("sessions", {})
            if session_id:
                sessions = payload.get("sessions", {})
                current = dict(sessions.get(str(session_id), {}))
                current.update(cleaned)
                sessions[str(session_id)] = current
                payload["sessions"] = sessions
            else:
                global_record = dict(payload.get("global", {}))
                global_record.update(cleaned)
                payload["global"] = global_record
            self._write_json_locked(self.preferences_path, payload)

        return self.get_preferences(session_id=session_id)

    def create_schedule(
        self,
        *,
        session_id: str,
        user_input: str,
        schedule_type: str,
        run_at: str = "",
        interval_seconds: int = 0,
        time_of_day: str = "",
        note: str = "",
        goal_id: str = "",
        project_id: str = "",
        todo_id: str = "",
    ) -> Dict[str, Any]:
        now = datetime.now()
        normalized_type = str(schedule_type or "").strip().lower() or "once"
        next_run = ""
        if normalized_type == "once":
            run_at_dt = _parse_iso(run_at) or (now + timedelta(seconds=settings.SCHEDULE_DEFAULT_LOOKAHEAD_SECONDS))
            next_run = run_at_dt.isoformat(timespec="seconds")
        elif normalized_type == "interval":
            seconds = max(int(interval_seconds or 0), 60)
            next_run = (now + timedelta(seconds=seconds)).isoformat(timespec="seconds")
            interval_seconds = seconds
        elif normalized_type == "daily":
            candidate = _parse_daily_time(time_of_day, now=now)
            next_run = candidate.isoformat(timespec="seconds")
        else:
            raise ValueError(f"Unsupported schedule type: {schedule_type}")

        with self._lock:
            schedules = self._read_jsonl_locked(self.schedules_path)
            record = {
                "schedule_id": _new_id("schedule"),
                "session_id": session_id,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "status": "active",
                "schedule_type": normalized_type,
                "user_input": _short_text(user_input, 500),
                "run_at": _short_text(run_at, 80),
                "interval_seconds": max(int(interval_seconds or 0), 0),
                "time_of_day": _short_text(time_of_day, 20),
                "next_run_at": next_run,
                "last_run_at": "",
                "last_job_id": "",
                "note": _short_text(note, 300),
                "goal_id": _short_text(goal_id, 120),
                "project_id": _short_text(project_id, 120),
                "todo_id": _short_text(todo_id, 120),
            }
            schedules.append(record)
            self._write_jsonl_locked(self.schedules_path, schedules)
            return dict(record)

    def load_schedules(
        self,
        *,
        session_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = 100,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            schedules = self._read_jsonl_locked(self.schedules_path)
        if session_id:
            schedules = [item for item in schedules if str(item.get("session_id", "")) == str(session_id)]
        if status:
            schedules = [item for item in schedules if str(item.get("status", "")) == str(status)]
        if limit is None:
            return schedules
        return schedules[-max(limit, 0):]

    def pause_schedule(self, schedule_id: str) -> Dict[str, Any]:
        return self._update_schedule_status(schedule_id, "paused")

    def resume_schedule(self, schedule_id: str) -> Dict[str, Any]:
        return self._update_schedule_status(schedule_id, "active", recompute_next=True)

    def delete_schedule(self, schedule_id: str) -> Dict[str, Any]:
        with self._lock:
            schedules = self._read_jsonl_locked(self.schedules_path)
            removed: Dict[str, Any] = {}
            remaining = []
            for item in schedules:
                if str(item.get("schedule_id", "")) == str(schedule_id):
                    removed = dict(item)
                    continue
                remaining.append(item)
            self._write_jsonl_locked(self.schedules_path, remaining)
        return removed

    def _update_schedule_status(
        self,
        schedule_id: str,
        status: str,
        *,
        recompute_next: bool = False,
    ) -> Dict[str, Any]:
        with self._lock:
            schedules = self._read_jsonl_locked(self.schedules_path)
            updated: Dict[str, Any] = {}
            for item in schedules:
                if str(item.get("schedule_id", "")) != str(schedule_id):
                    continue
                item["status"] = status
                item["updated_at"] = _now_iso()
                if recompute_next and status == "active":
                    item["next_run_at"] = _compute_next_schedule_run(item, from_time=datetime.now())
                updated = dict(item)
                break
            self._write_jsonl_locked(self.schedules_path, schedules)
        return updated

    def release_due_schedules(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        max_count = max(int(limit or settings.SCHEDULER_RELEASE_LIMIT), 1)
        released: List[Dict[str, Any]] = []
        now = datetime.now()

        with self._lock:
            schedules = self._read_jsonl_locked(self.schedules_path)
            for item in schedules:
                if len(released) >= max_count:
                    break
                if str(item.get("status", "")) != "active":
                    continue
                next_run_at = _parse_iso(item.get("next_run_at"))
                if next_run_at is None or next_run_at > now:
                    continue

                job = self._submit_job_locked(
                    session_id=str(item.get("session_id", "") or ""),
                    user_input=str(item.get("user_input", "") or ""),
                    is_special_command=False,
                    trigger_source="schedule",
                    schedule_id=str(item.get("schedule_id", "") or ""),
                    goal_id=str(item.get("goal_id", "") or ""),
                    project_id=str(item.get("project_id", "") or ""),
                    todo_id=str(item.get("todo_id", "") or ""),
                )
                item["last_run_at"] = _now_iso()
                item["last_job_id"] = str(job.get("job_id", "") or "")
                item["updated_at"] = item["last_run_at"]
                next_run = _compute_next_schedule_run(item, from_time=now)
                if next_run:
                    item["next_run_at"] = next_run
                else:
                    item["next_run_at"] = ""
                    item["status"] = "completed"
                released.append({
                    "schedule_id": str(item.get("schedule_id", "") or ""),
                    "job_id": str(job.get("job_id", "") or ""),
                    "session_id": str(item.get("session_id", "") or ""),
                    "user_input": str(item.get("user_input", "") or ""),
                })

            if schedules:
                self._write_jsonl_locked(self.schedules_path, schedules)

        return released

    def get_schedule_summary(self) -> Dict[str, int]:
        schedules = self.load_schedules(limit=None)
        summary = {"active": 0, "paused": 0, "completed": 0}
        for item in schedules:
            status = str(item.get("status", "") or "")
            if status in summary:
                summary[status] += 1
        return summary

    def create_notification(
        self,
        *,
        session_id: str,
        title: str,
        message: str,
        level: str = "info",
        category: str = "system",
        job_id: str = "",
        requires_action: bool = False,
    ) -> Dict[str, Any]:
        record = {
            "notification_id": _new_id("notice"),
            "session_id": session_id,
            "job_id": job_id,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "level": _short_text(level, 20),
            "category": _short_text(category, 40),
            "title": _short_text(title, 160),
            "message": _short_text(message, 500),
            "requires_action": bool(requires_action),
            "read": False,
        }
        with self._lock:
            records = self._read_jsonl_locked(self.notifications_path)
            records.append(record)
            max_records = max(int(settings.NOTIFICATION_HISTORY_LIMIT), 20)
            if len(records) > max_records:
                records = records[-max_records:]
            self._write_jsonl_locked(self.notifications_path, records)
        return dict(record)

    def load_notifications(
        self,
        *,
        session_id: Optional[str] = None,
        unread_only: bool = False,
        limit: Optional[int] = 100,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            records = self._read_jsonl_locked(self.notifications_path)
        if session_id:
            records = [item for item in records if str(item.get("session_id", "")) == str(session_id)]
        if unread_only:
            records = [item for item in records if not bool(item.get("read", False))]
        if limit is None:
            return records
        return records[-max(limit, 0):]

    def mark_notification_read(self, notification_id: str) -> Dict[str, Any]:
        with self._lock:
            records = self._read_jsonl_locked(self.notifications_path)
            updated: Dict[str, Any] = {}
            for item in records:
                if str(item.get("notification_id", "")) != str(notification_id):
                    continue
                item["read"] = True
                item["updated_at"] = _now_iso()
                updated = dict(item)
                break
            if records:
                self._write_jsonl_locked(self.notifications_path, records)
        return updated

    def mark_notifications_read(self, session_id: Optional[str] = None) -> int:
        updated = 0
        with self._lock:
            records = self._read_jsonl_locked(self.notifications_path)
            for item in records:
                if session_id and str(item.get("session_id", "")) != str(session_id):
                    continue
                if bool(item.get("read", False)):
                    continue
                item["read"] = True
                item["updated_at"] = _now_iso()
                updated += 1
            if records:
                self._write_jsonl_locked(self.notifications_path, records)
        return updated


_runtime_state_store: Optional[RuntimeStateStore] = None


def get_runtime_state_store() -> RuntimeStateStore:
    global _runtime_state_store
    if _runtime_state_store is None:
        _runtime_state_store = RuntimeStateStore()
    return _runtime_state_store
