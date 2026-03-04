"""
Independent artifact catalog for cross-job reuse and lookup.
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


def _new_id() -> str:
    return f"catalog_{uuid.uuid4().hex[:12]}"


def _short(value: Any, limit: int = 500) -> str:
    return str(value or "")[:limit]


def _fingerprint(item: Dict[str, Any]) -> str:
    if item.get("artifact_id"):
        return f"id:{item.get('artifact_id')}"
    if item.get("path"):
        return f"path:{item.get('path')}"
    return f"name:{item.get('name', '')}:{item.get('job_id', '')}"


class ArtifactStore:
    def __init__(self, catalog_path: Optional[Path] = None):
        self.catalog_path = Path(catalog_path) if catalog_path else settings.DATA_DIR / "artifact_store" / "catalog.jsonl"
        self._lock = threading.Lock()

    def _ensure_dir(self) -> None:
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)

    def _read_locked(self) -> List[Dict[str, Any]]:
        if not self.catalog_path.exists():
            return []
        records: List[Dict[str, Any]] = []
        with self.catalog_path.open("r", encoding="utf-8") as handle:
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

    def _write_locked(self, records: List[Dict[str, Any]]) -> None:
        self._ensure_dir()
        with self.catalog_path.open("w", encoding="utf-8") as handle:
            for item in records:
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True))
                handle.write("\n")

    def record_artifacts(
        self,
        *,
        session_id: str,
        job_id: str,
        artifacts: List[Dict[str, Any]],
        goal_id: str = "",
        project_id: str = "",
        todo_id: str = "",
    ) -> List[Dict[str, Any]]:
        recorded: List[Dict[str, Any]] = []
        with self._lock:
            existing = self._read_locked()
            existing_keys = {
                (str(item.get("job_id", "")), _fingerprint(item))
                for item in existing
                if isinstance(item, dict)
            }
            for artifact in artifacts or []:
                if not isinstance(artifact, dict):
                    continue
                candidate = {
                    "catalog_id": _new_id(),
                    "artifact_id": _short(artifact.get("artifact_id", ""), 120),
                    "session_id": _short(session_id, 120),
                    "job_id": _short(job_id, 120),
                    "goal_id": _short(goal_id, 120),
                    "project_id": _short(project_id, 120),
                    "todo_id": _short(todo_id, 120),
                    "created_at": _now_iso(),
                    "artifact_type": _short(artifact.get("artifact_type", ""), 60),
                    "name": _short(artifact.get("name", ""), 240),
                    "path": _short(artifact.get("path", ""), 500),
                    "preview": _short(artifact.get("preview", ""), 500),
                    "tool_name": _short(artifact.get("tool_name", ""), 120),
                    "task_id": _short(artifact.get("task_id", ""), 120),
                }
                key = (candidate["job_id"], _fingerprint(candidate))
                if key in existing_keys:
                    continue
                existing.append(candidate)
                existing_keys.add(key)
                recorded.append(dict(candidate))
            if recorded:
                if len(existing) > 1000:
                    existing = existing[-1000:]
                self._write_locked(existing)
        return recorded

    def search_artifacts(
        self,
        *,
        session_id: Optional[str] = None,
        goal_id: Optional[str] = None,
        project_id: Optional[str] = None,
        todo_id: Optional[str] = None,
        query: str = "",
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        needle = query.lower().strip()
        with self._lock:
            records = self._read_locked()
        filtered = []
        for item in records:
            if session_id and str(item.get("session_id", "")) != str(session_id):
                continue
            if goal_id and str(item.get("goal_id", "")) != str(goal_id):
                continue
            if project_id and str(item.get("project_id", "")) != str(project_id):
                continue
            if todo_id and str(item.get("todo_id", "")) != str(todo_id):
                continue
            if needle:
                haystack = " ".join(
                    [
                        str(item.get("name", "") or ""),
                        str(item.get("path", "") or ""),
                        str(item.get("preview", "") or ""),
                        str(item.get("artifact_type", "") or ""),
                    ]
                ).lower()
                if needle not in haystack:
                    continue
            filtered.append(item)
        return [dict(item) for item in filtered[-max(limit, 1):]]


_artifact_store: Optional[ArtifactStore] = None


def get_artifact_store() -> ArtifactStore:
    global _artifact_store
    if _artifact_store is None:
        _artifact_store = ArtifactStore()
    return _artifact_store
