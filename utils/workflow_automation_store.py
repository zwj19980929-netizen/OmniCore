"""
Automation helpers for reusable templates and lightweight directory watchers.
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


class WorkflowAutomationStore:
    def __init__(self, state_dir: Optional[Path] = None):
        self.state_dir = Path(state_dir) if state_dir else settings.DATA_DIR / "automation"
        self.templates_path = self.state_dir / "templates.jsonl"
        self.directory_watches_path = self.state_dir / "directory_watches.jsonl"
        self.watch_events_path = self.state_dir / "directory_watch_events.jsonl"
        self._lock = threading.Lock()

    def _ensure_dir(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _read_jsonl_locked(self, path: Path) -> List[Dict[str, Any]]:
        if not path.exists():
            return []
        rows: List[Dict[str, Any]] = []
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
                    rows.append(item)
        return rows

    def _write_jsonl_locked(self, path: Path, rows: List[Dict[str, Any]]) -> None:
        self._ensure_dir()
        with path.open("w", encoding="utf-8") as handle:
            for item in rows:
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True))
                handle.write("\n")

    def create_template(
        self,
        *,
        session_id: str,
        name: str,
        user_input: str,
        goal_id: str = "",
        project_id: str = "",
        todo_id: str = "",
        source_job_id: str = "",
        notes: str = "",
    ) -> Dict[str, Any]:
        record = {
            "template_id": _new_id("template"),
            "session_id": _short(session_id, 120),
            "name": _short(name, 200),
            "user_input": _short(user_input, 1000),
            "goal_id": _short(goal_id, 120),
            "project_id": _short(project_id, 120),
            "todo_id": _short(todo_id, 120),
            "source_job_id": _short(source_job_id, 120),
            "notes": _short(notes, 500),
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        with self._lock:
            rows = self._read_jsonl_locked(self.templates_path)
            rows.append(record)
            self._write_jsonl_locked(self.templates_path, rows)
        return dict(record)

    def list_templates(self, *, session_id: Optional[str] = None, limit: Optional[int] = 100) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._read_jsonl_locked(self.templates_path)
        if session_id:
            rows = [item for item in rows if str(item.get("session_id", "")) == str(session_id)]
        if limit is None:
            return rows
        return rows[-max(limit, 0):]

    def get_template(self, template_id: str) -> Dict[str, Any]:
        with self._lock:
            rows = self._read_jsonl_locked(self.templates_path)
        for item in rows:
            if str(item.get("template_id", "")) == str(template_id):
                return dict(item)
        return {}

    def delete_template(self, template_id: str) -> Dict[str, Any]:
        with self._lock:
            rows = self._read_jsonl_locked(self.templates_path)
            kept: List[Dict[str, Any]] = []
            removed: Dict[str, Any] = {}
            for item in rows:
                if str(item.get("template_id", "")) == str(template_id):
                    removed = dict(item)
                    continue
                kept.append(item)
            self._write_jsonl_locked(self.templates_path, kept)
        return removed

    def create_directory_watch(
        self,
        *,
        session_id: str,
        directory_path: str,
        template_id: str = "",
        user_input: str = "",
        goal_id: str = "",
        project_id: str = "",
        todo_id: str = "",
        note: str = "",
    ) -> Dict[str, Any]:
        target_dir = str(Path(directory_path).expanduser())
        path_obj = Path(target_dir)
        known_entries: List[str] = []
        if path_obj.is_dir():
            known_entries = sorted(str(item) for item in path_obj.iterdir() if item.is_file())

        record = {
            "watch_id": _new_id("watch"),
            "session_id": _short(session_id, 120),
            "directory_path": target_dir,
            "template_id": _short(template_id, 120),
            "user_input": _short(user_input, 1000),
            "goal_id": _short(goal_id, 120),
            "project_id": _short(project_id, 120),
            "todo_id": _short(todo_id, 120),
            "note": _short(note, 500),
            "status": "waiting_for_event",
            "known_entries": known_entries,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "last_triggered_at": "",
            "last_event_path": "",
        }
        with self._lock:
            rows = self._read_jsonl_locked(self.directory_watches_path)
            rows.append(record)
            self._write_jsonl_locked(self.directory_watches_path, rows)
        return dict(record)

    def list_directory_watches(
        self,
        *,
        session_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = 100,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._read_jsonl_locked(self.directory_watches_path)
        if session_id:
            rows = [item for item in rows if str(item.get("session_id", "")) == str(session_id)]
        if status:
            rows = [item for item in rows if str(item.get("status", "")) == str(status)]
        if limit is None:
            return rows
        return rows[-max(limit, 0):]

    def update_directory_watch_status(self, watch_id: str, status: str) -> Dict[str, Any]:
        with self._lock:
            rows = self._read_jsonl_locked(self.directory_watches_path)
            updated: Dict[str, Any] = {}
            for item in rows:
                if str(item.get("watch_id", "")) != str(watch_id):
                    continue
                item["status"] = _short(status, 40)
                item["updated_at"] = _now_iso()
                updated = dict(item)
                break
            self._write_jsonl_locked(self.directory_watches_path, rows)
        return updated

    def delete_directory_watch(self, watch_id: str) -> Dict[str, Any]:
        with self._lock:
            rows = self._read_jsonl_locked(self.directory_watches_path)
            kept: List[Dict[str, Any]] = []
            removed: Dict[str, Any] = {}
            for item in rows:
                if str(item.get("watch_id", "")) == str(watch_id):
                    removed = dict(item)
                    continue
                kept.append(item)
            self._write_jsonl_locked(self.directory_watches_path, kept)
        return removed

    def poll_directory_watch_events(self, *, limit: int = 10) -> List[Dict[str, Any]]:
        released: List[Dict[str, Any]] = []
        with self._lock:
            rows = self._read_jsonl_locked(self.directory_watches_path)
            event_rows = self._read_jsonl_locked(self.watch_events_path)

            for item in rows:
                if len(released) >= max(limit, 1):
                    break
                if str(item.get("status", "")) != "waiting_for_event":
                    continue
                directory = Path(str(item.get("directory_path", "") or "")).expanduser()
                if not directory.is_dir():
                    continue
                known = {
                    str(entry)
                    for entry in item.get("known_entries", []) or []
                    if str(entry).strip()
                }
                current = sorted(str(entry) for entry in directory.iterdir() if entry.is_file())
                new_files = [entry for entry in current if entry not in known]
                if not new_files:
                    item["known_entries"] = current
                    continue

                for file_path in new_files:
                    if len(released) >= max(limit, 1):
                        break
                    event = {
                        "event_id": _new_id("event"),
                        "watch_id": str(item.get("watch_id", "") or ""),
                        "session_id": str(item.get("session_id", "") or ""),
                        "template_id": str(item.get("template_id", "") or ""),
                        "user_input": str(item.get("user_input", "") or ""),
                        "goal_id": str(item.get("goal_id", "") or ""),
                        "project_id": str(item.get("project_id", "") or ""),
                        "todo_id": str(item.get("todo_id", "") or ""),
                        "file_path": file_path,
                        "created_at": _now_iso(),
                    }
                    released.append(event)
                    event_rows.append(event)
                    item["last_triggered_at"] = event["created_at"]
                    item["last_event_path"] = file_path

                item["known_entries"] = current
                item["updated_at"] = _now_iso()

            self._write_jsonl_locked(self.directory_watches_path, rows)
            if released:
                if len(event_rows) > 500:
                    event_rows = event_rows[-500:]
                self._write_jsonl_locked(self.watch_events_path, event_rows)

        return released

    def list_directory_watch_events(
        self,
        *,
        session_id: Optional[str] = None,
        limit: Optional[int] = 100,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._read_jsonl_locked(self.watch_events_path)
        if session_id:
            rows = [item for item in rows if str(item.get("session_id", "")) == str(session_id)]
        if limit is None:
            return rows
        return rows[-max(limit, 0):]


_workflow_automation_store: Optional[WorkflowAutomationStore] = None


def get_workflow_automation_store() -> WorkflowAutomationStore:
    global _workflow_automation_store
    if _workflow_automation_store is None:
        _workflow_automation_store = WorkflowAutomationStore()
    return _workflow_automation_store
