"""
Persistence helpers for session/job/artifact runtime state.
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


def _short_text(value: Any, limit: int = 300) -> str:
    text = str(value or "")
    return text[:limit]


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


class RuntimeStateStore:
    def __init__(self, state_dir: Optional[Path] = None):
        self.state_dir = Path(state_dir) if state_dir else settings.DATA_DIR / "runtime_state"
        self.sessions_path = self.state_dir / "sessions.json"
        self.jobs_path = self.state_dir / "jobs.jsonl"
        self.artifacts_path = self.state_dir / "artifacts.jsonl"
        self.queue_path = self.state_dir / "job_queue.jsonl"
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

    def submit_job(
        self,
        *,
        session_id: str,
        user_input: str,
        is_special_command: bool = False,
    ) -> Dict[str, Any]:
        with self._lock:
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
                item["task_count"] = len(tasks or [])
                item["tasks_completed"] = sum(
                    1 for task in tasks or []
                    if isinstance(task, dict) and task.get("status") == "completed"
                )
                item["is_special_command"] = bool(is_special_command)
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

    def load_jobs(
        self,
        *,
        session_id: Optional[str] = None,
        limit: Optional[int] = 50,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = self._read_jsonl_locked(self.jobs_path)
        if session_id:
            jobs = [item for item in jobs if str(item.get("session_id", "")) == str(session_id)]
        if limit is None:
            return jobs
        return jobs[-max(limit, 0):]

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
        }
        for item in queue_records:
            status = str(item.get("status", "") or "")
            if status in summary:
                summary[status] += 1
        return summary


_runtime_state_store: Optional[RuntimeStateStore] = None


def get_runtime_state_store() -> RuntimeStateStore:
    global _runtime_state_store
    if _runtime_state_store is None:
        _runtime_state_store = RuntimeStateStore()
    return _runtime_state_store
