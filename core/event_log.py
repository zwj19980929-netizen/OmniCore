"""
S3: Session Event Sourcing — append-only event log for session/job lifecycle.

Write path: memory queue → batch flush to JSONL.
Read path: load events → rebuild job state / conversation history.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import settings
from utils.logger import log_agent_action, log_warning


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    JOB_SUBMITTED = "job_submitted"
    JOB_STATUS_CHANGED = "job_status_changed"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    PLAN_CREATED = "plan_created"
    PLAN_UPDATED = "plan_updated"
    ARTIFACT_CREATED = "artifact_created"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    CHECKPOINT_SAVED = "checkpoint_saved"
    METADATA_UPDATED = "metadata_updated"


# ---------------------------------------------------------------------------
# SessionEvent dataclass
# ---------------------------------------------------------------------------

@dataclass
class SessionEvent:
    event_type: str
    session_id: str
    data: Dict[str, Any] = field(default_factory=dict)
    job_id: Optional[str] = None
    parent_event_id: Optional[str] = None
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_jsonl(self) -> str:
        """Serialize to a single JSONL line."""
        payload = asdict(self)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_jsonl(cls, line: str) -> SessionEvent:
        """Deserialize from a JSONL line."""
        raw = json.loads(line)
        return cls(
            event_id=raw.get("event_id", uuid.uuid4().hex[:16]),
            event_type=raw.get("event_type", ""),
            session_id=raw.get("session_id", ""),
            job_id=raw.get("job_id"),
            timestamp=raw.get("timestamp", ""),
            data=raw.get("data", {}),
            parent_event_id=raw.get("parent_event_id"),
        )


# ---------------------------------------------------------------------------
# EventWriter — append-only write path
# ---------------------------------------------------------------------------

class EventWriter:
    """Memory-queued, batch-flushing event writer."""

    def __init__(self, events_dir: Optional[Path] = None, flush_interval: float = 0):
        self._events_dir = events_dir or (settings.DATA_DIR / "events")
        self._flush_interval = flush_interval or getattr(
            settings, "SESSION_EVENT_FLUSH_INTERVAL", 5
        )
        self._queue: List[SessionEvent] = []
        self._event_ids: set[str] = set()
        self._lock = threading.Lock()
        self._last_flush: float = time.monotonic()

    # -- public API --

    def append(self, event: SessionEvent) -> None:
        """Enqueue an event. Triggers flush if threshold reached."""
        with self._lock:
            if event.event_id in self._event_ids:
                return  # dedupe
            self._event_ids.add(event.event_id)
            self._queue.append(event)
            if self._should_flush():
                self._flush_locked()

    def flush(self) -> int:
        """Force-flush all queued events. Returns count flushed."""
        with self._lock:
            return self._flush_locked()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    # -- internal --

    def _should_flush(self) -> bool:
        if len(self._queue) >= 50:
            return True
        elapsed = time.monotonic() - self._last_flush
        return elapsed >= self._flush_interval

    def _flush_locked(self) -> int:
        if not self._queue:
            return 0

        # Group events by session_id for writing to separate files
        by_session: Dict[str, List[SessionEvent]] = {}
        for ev in self._queue:
            by_session.setdefault(ev.session_id, []).append(ev)

        flushed = 0
        for session_id, events in by_session.items():
            if not session_id:
                continue
            path = self._events_dir / f"{session_id}.jsonl"
            try:
                self._events_dir.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    for ev in events:
                        fh.write(ev.to_jsonl() + "\n")
                        flushed += 1
                # Set file permissions to 0o600 (owner read/write only)
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
            except OSError as exc:
                log_warning(f"EventWriter flush failed for session {session_id}: {exc}")

        self._queue.clear()
        self._last_flush = time.monotonic()
        return flushed

    def _event_path(self, session_id: str) -> Path:
        return self._events_dir / f"{session_id}.jsonl"


# ---------------------------------------------------------------------------
# EventReader — read / rebuild path
# ---------------------------------------------------------------------------

class EventReader:
    """Read events and rebuild state from append-only JSONL logs."""

    def __init__(self, events_dir: Optional[Path] = None):
        self._events_dir = events_dir or (settings.DATA_DIR / "events")

    def load_session(self, session_id: str) -> List[SessionEvent]:
        """Load all events for a session. Tolerates corrupted lines."""
        path = self._events_dir / f"{session_id}.jsonl"
        if not path.exists():
            return []
        events: List[SessionEvent] = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line_no, raw in enumerate(fh, 1):
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        events.append(SessionEvent.from_jsonl(line))
                    except (json.JSONDecodeError, KeyError, TypeError) as exc:
                        log_warning(
                            f"EventReader: skipped corrupted line {line_no} "
                            f"in {path.name}: {exc}"
                        )
        except OSError as exc:
            log_warning(f"EventReader: failed to read {path}: {exc}")
        return events

    def load_job_events(self, session_id: str, job_id: str) -> List[SessionEvent]:
        """Load events for a specific job within a session."""
        all_events = self.load_session(session_id)
        return [
            ev for ev in all_events
            if ev.job_id == job_id or ev.event_type in (
                EventType.SESSION_START, EventType.SESSION_END,
                EventType.METADATA_UPDATED,
            )
        ]

    def rebuild_job_state(self, events: List[SessionEvent], job_id: str) -> Dict[str, Any]:
        """Rebuild job final state from event sequence.

        Returns a dict compatible with the job record in runtime_state_store.
        """
        state: Dict[str, Any] = {
            "job_id": job_id,
            "status": "unknown",
            "tasks": [],
            "artifacts": [],
            "policy_decisions": [],
            "plan": None,
            "error": "",
            "intent": "",
        }
        task_map: Dict[str, Dict[str, Any]] = {}

        for ev in events:
            if ev.job_id != job_id and ev.event_type not in (
                EventType.SESSION_START, EventType.SESSION_END,
            ):
                continue

            etype = ev.event_type
            data = ev.data or {}

            if etype == EventType.JOB_SUBMITTED:
                state["status"] = "queued"
                state["user_input"] = data.get("user_input", "")
                state["session_id"] = ev.session_id
                state["created_at"] = ev.timestamp

            elif etype == EventType.JOB_STATUS_CHANGED:
                state["status"] = data.get("new_status", state["status"])
                if data.get("intent"):
                    state["intent"] = data["intent"]
                if data.get("error"):
                    state["error"] = data["error"]
                if data.get("output"):
                    state["output_preview"] = data["output"]

            elif etype == EventType.TASK_STARTED:
                tid = data.get("task_id", "")
                task_map.setdefault(tid, {"task_id": tid})
                task_map[tid]["status"] = "running"
                task_map[tid].update({
                    k: data[k] for k in ("task_type", "description", "tool_name")
                    if k in data
                })

            elif etype == EventType.TASK_COMPLETED:
                tid = data.get("task_id", "")
                task_map.setdefault(tid, {"task_id": tid})
                task_map[tid]["status"] = "completed"
                if "result_preview" in data:
                    task_map[tid]["result_preview"] = data["result_preview"]

            elif etype == EventType.TASK_FAILED:
                tid = data.get("task_id", "")
                task_map.setdefault(tid, {"task_id": tid})
                task_map[tid]["status"] = "failed"
                task_map[tid]["error"] = data.get("error", "")
                task_map[tid]["failure_type"] = data.get("failure_type", "")

            elif etype == EventType.PLAN_CREATED:
                state["plan"] = data.get("plan_summary", "")

            elif etype == EventType.PLAN_UPDATED:
                state["plan"] = data.get("plan_summary", state.get("plan", ""))

            elif etype == EventType.ARTIFACT_CREATED:
                state["artifacts"].append(data)

            elif etype == EventType.APPROVAL_REQUESTED:
                state["policy_decisions"].append({
                    "task_id": data.get("task_id", ""),
                    "action": "requested",
                    "reason": data.get("reason", ""),
                    "timestamp": ev.timestamp,
                })

            elif etype == EventType.APPROVAL_RESOLVED:
                state["policy_decisions"].append({
                    "task_id": data.get("task_id", ""),
                    "action": data.get("decision", ""),
                    "reason": data.get("reason", ""),
                    "timestamp": ev.timestamp,
                })

        state["tasks"] = list(task_map.values())
        return state

    def rebuild_conversation(
        self, events: List[SessionEvent], job_id: str,
    ) -> List[Dict[str, str]]:
        """Rebuild minimal conversation log from events for resume context.

        Returns a list of {role, content} dicts suitable for injection.
        """
        messages: List[Dict[str, str]] = []
        for ev in events:
            if ev.job_id != job_id:
                continue
            data = ev.data or {}

            if ev.event_type == EventType.JOB_SUBMITTED:
                messages.append({
                    "role": "user",
                    "content": data.get("user_input", ""),
                })

            elif ev.event_type == EventType.TASK_COMPLETED:
                preview = data.get("result_preview", "")
                if preview:
                    messages.append({
                        "role": "assistant",
                        "content": f"[Task {data.get('task_id', '')} completed] {preview}",
                    })

            elif ev.event_type == EventType.PLAN_CREATED:
                messages.append({
                    "role": "system",
                    "content": f"[Plan] {data.get('plan_summary', '')}",
                })

        return messages

    def scan_session_metadata(self, session_id: str) -> Dict[str, Any]:
        """Fast metadata scan: read head + tail of file to extract session info.

        Looks for session_start and metadata_updated events without loading
        the full file into memory.
        """
        path = self._events_dir / f"{session_id}.jsonl"
        if not path.exists():
            return {}

        metadata: Dict[str, Any] = {"session_id": session_id}
        chunk_size = 64 * 1024  # 64KB

        try:
            file_size = path.stat().st_size
            with path.open("r", encoding="utf-8") as fh:
                # Read head
                head = fh.read(chunk_size)
                for line in head.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = SessionEvent.from_jsonl(line)
                        if ev.event_type == EventType.SESSION_START:
                            metadata["created_at"] = ev.timestamp
                            metadata.update(ev.data)
                        elif ev.event_type == EventType.METADATA_UPDATED:
                            metadata.update(ev.data)
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue

                # Read tail if file is larger than head chunk
                if file_size > chunk_size:
                    fh.seek(max(0, file_size - chunk_size))
                    fh.readline()  # skip partial line
                    tail = fh.read()
                    for line in tail.split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = SessionEvent.from_jsonl(line)
                            if ev.event_type == EventType.METADATA_UPDATED:
                                metadata.update(ev.data)
                            elif ev.event_type == EventType.SESSION_END:
                                metadata["ended_at"] = ev.timestamp
                        except (json.JSONDecodeError, KeyError, TypeError):
                            continue

        except OSError as exc:
            log_warning(f"EventReader: metadata scan failed for {session_id}: {exc}")

        return metadata

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List all sessions with basic metadata (fast scan)."""
        if not self._events_dir.exists():
            return []
        sessions = []
        for path in sorted(self._events_dir.glob("*.jsonl")):
            session_id = path.stem
            meta = self.scan_session_metadata(session_id)
            if meta:
                sessions.append(meta)
        return sessions

    def has_events(self, session_id: str) -> bool:
        path = self._events_dir / f"{session_id}.jsonl"
        return path.exists() and path.stat().st_size > 0

    def last_event(self, session_id: str) -> Optional[SessionEvent]:
        """Return the last event in a session log (for resume detection)."""
        path = self._events_dir / f"{session_id}.jsonl"
        if not path.exists():
            return None
        last_line = ""
        try:
            with path.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    stripped = raw.strip()
                    if stripped:
                        last_line = stripped
        except OSError:
            return None
        if not last_line:
            return None
        try:
            return SessionEvent.from_jsonl(last_line)
        except (json.JSONDecodeError, KeyError, TypeError):
            return None


# ---------------------------------------------------------------------------
# Module-level helpers — emit events if enabled
# ---------------------------------------------------------------------------

_writer_instance: Optional[EventWriter] = None
_writer_lock = threading.Lock()


def get_event_writer() -> EventWriter:
    """Get or create the singleton EventWriter."""
    global _writer_instance
    if _writer_instance is None:
        with _writer_lock:
            if _writer_instance is None:
                _writer_instance = EventWriter()
    return _writer_instance


def get_event_reader() -> EventReader:
    """Create a new EventReader (stateless, no singleton needed)."""
    return EventReader()


def _is_enabled() -> bool:
    return getattr(settings, "SESSION_EVENT_LOG_ENABLED", False)


def emit_event(
    event_type: str,
    session_id: str,
    job_id: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
    parent_event_id: Optional[str] = None,
) -> Optional[SessionEvent]:
    """Convenience: create and enqueue an event if event sourcing is enabled.

    Returns the event if emitted, None if disabled.
    """
    if not _is_enabled():
        return None
    if not session_id:
        return None

    event = SessionEvent(
        event_type=event_type,
        session_id=session_id,
        job_id=job_id,
        data=data or {},
        parent_event_id=parent_event_id,
    )
    try:
        get_event_writer().append(event)
    except Exception as exc:
        log_warning(f"emit_event failed: {exc}")
        return None
    return event


def flush_events() -> int:
    """Flush any pending events to disk."""
    if not _is_enabled():
        return 0
    try:
        return get_event_writer().flush()
    except Exception:
        return 0
