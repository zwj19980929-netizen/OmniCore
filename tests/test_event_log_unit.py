"""
S3: Unit tests for core/event_log.py — SessionEvent, EventWriter, EventReader.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from core.event_log import (
    EventReader,
    EventType,
    EventWriter,
    SessionEvent,
    emit_event,
    flush_events,
    get_event_writer,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_events_dir(tmp_path):
    d = tmp_path / "events"
    d.mkdir()
    return d


@pytest.fixture
def writer(tmp_events_dir):
    return EventWriter(events_dir=tmp_events_dir, flush_interval=0.1)


@pytest.fixture
def reader(tmp_events_dir):
    return EventReader(events_dir=tmp_events_dir)


def _make_event(
    session_id="sess_1",
    job_id="job_1",
    event_type=EventType.JOB_SUBMITTED,
    data=None,
):
    return SessionEvent(
        event_type=event_type,
        session_id=session_id,
        job_id=job_id,
        data=data or {},
    )


# ===========================================================================
# SessionEvent serialization
# ===========================================================================

class TestSessionEvent:
    def test_to_jsonl_roundtrip(self):
        ev = _make_event(data={"user_input": "hello"})
        line = ev.to_jsonl()
        restored = SessionEvent.from_jsonl(line)
        assert restored.event_type == ev.event_type
        assert restored.session_id == ev.session_id
        assert restored.job_id == ev.job_id
        assert restored.data == ev.data
        assert restored.event_id == ev.event_id

    def test_from_jsonl_missing_fields(self):
        line = json.dumps({"event_type": "test", "session_id": "s1"})
        ev = SessionEvent.from_jsonl(line)
        assert ev.event_type == "test"
        assert ev.session_id == "s1"
        assert ev.job_id is None
        assert ev.data == {}

    def test_event_id_auto_generated(self):
        ev1 = _make_event()
        ev2 = _make_event()
        assert ev1.event_id != ev2.event_id
        assert len(ev1.event_id) == 16

    def test_timestamp_auto_generated(self):
        ev = _make_event()
        assert "T" in ev.timestamp  # ISO format


# ===========================================================================
# EventWriter
# ===========================================================================

class TestEventWriter:
    def test_append_and_flush(self, writer, tmp_events_dir):
        ev = _make_event()
        writer.append(ev)
        count = writer.flush()
        assert count == 1
        path = tmp_events_dir / "sess_1.jsonl"
        assert path.exists()
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        restored = SessionEvent.from_jsonl(lines[0])
        assert restored.event_id == ev.event_id

    def test_dedupe(self, writer):
        ev = _make_event()
        writer.append(ev)
        writer.append(ev)  # duplicate
        count = writer.flush()
        assert count == 1

    def test_multiple_sessions(self, writer, tmp_events_dir):
        ev1 = _make_event(session_id="s1")
        ev2 = _make_event(session_id="s2")
        writer.append(ev1)
        writer.append(ev2)
        writer.flush()
        assert (tmp_events_dir / "s1.jsonl").exists()
        assert (tmp_events_dir / "s2.jsonl").exists()

    def test_auto_flush_on_count(self, tmp_events_dir):
        w = EventWriter(events_dir=tmp_events_dir, flush_interval=9999)
        for i in range(50):
            w.append(_make_event(data={"i": i}))
        # 50 events should trigger auto-flush
        path = tmp_events_dir / "sess_1.jsonl"
        assert path.exists()

    def test_empty_session_id_skipped(self, writer, tmp_events_dir):
        ev = _make_event(session_id="")
        writer.append(ev)
        count = writer.flush()
        assert count == 0

    def test_pending_count(self, writer):
        assert writer.pending_count == 0
        writer.append(_make_event())
        assert writer.pending_count == 1
        writer.flush()
        assert writer.pending_count == 0

    def test_file_permissions(self, writer, tmp_events_dir):
        writer.append(_make_event())
        writer.flush()
        path = tmp_events_dir / "sess_1.jsonl"
        mode = oct(path.stat().st_mode & 0o777)
        assert mode == "0o600"

    def test_append_to_existing_file(self, writer, tmp_events_dir):
        ev1 = _make_event(data={"n": 1})
        writer.append(ev1)
        writer.flush()
        ev2 = _make_event(data={"n": 2})
        writer.append(ev2)
        writer.flush()
        path = tmp_events_dir / "sess_1.jsonl"
        lines = [l for l in path.read_text(encoding="utf-8").strip().split("\n") if l]
        assert len(lines) == 2


# ===========================================================================
# EventReader
# ===========================================================================

class TestEventReader:
    def _write_events(self, tmp_events_dir, events):
        writer = EventWriter(events_dir=tmp_events_dir, flush_interval=9999)
        for ev in events:
            writer.append(ev)
        writer.flush()

    def test_load_session(self, reader, tmp_events_dir):
        events = [
            _make_event(event_type=EventType.JOB_SUBMITTED, data={"user_input": "hi"}),
            _make_event(event_type=EventType.TASK_STARTED, data={"task_id": "t1"}),
        ]
        self._write_events(tmp_events_dir, events)
        loaded = reader.load_session("sess_1")
        assert len(loaded) == 2
        assert loaded[0].event_type == EventType.JOB_SUBMITTED
        assert loaded[1].event_type == EventType.TASK_STARTED

    def test_load_session_nonexistent(self, reader):
        loaded = reader.load_session("no_such_session")
        assert loaded == []

    def test_corrupted_line_tolerance(self, reader, tmp_events_dir):
        path = tmp_events_dir / "sess_1.jsonl"
        good = _make_event(data={"ok": True})
        path.write_text(
            good.to_jsonl() + "\n"
            + "THIS IS NOT JSON\n"
            + good.to_jsonl().replace(good.event_id, "other_id") + "\n",
            encoding="utf-8",
        )
        loaded = reader.load_session("sess_1")
        assert len(loaded) == 2  # corrupted line skipped

    def test_load_job_events(self, reader, tmp_events_dir):
        events = [
            _make_event(job_id="j1", event_type=EventType.TASK_STARTED),
            _make_event(job_id="j2", event_type=EventType.TASK_STARTED),
            _make_event(job_id="j1", event_type=EventType.TASK_COMPLETED),
        ]
        self._write_events(tmp_events_dir, events)
        j1_events = reader.load_job_events("sess_1", "j1")
        assert len(j1_events) == 2
        assert all(ev.job_id == "j1" for ev in j1_events)

    def test_has_events(self, reader, tmp_events_dir):
        assert not reader.has_events("sess_1")
        self._write_events(tmp_events_dir, [_make_event()])
        assert reader.has_events("sess_1")

    def test_last_event(self, reader, tmp_events_dir):
        events = [
            _make_event(event_type=EventType.JOB_SUBMITTED),
            _make_event(event_type=EventType.TASK_COMPLETED),
        ]
        self._write_events(tmp_events_dir, events)
        last = reader.last_event("sess_1")
        assert last is not None
        assert last.event_type == EventType.TASK_COMPLETED

    def test_last_event_nonexistent(self, reader):
        assert reader.last_event("no_such") is None


# ===========================================================================
# EventReader — state rebuild
# ===========================================================================

class TestRebuildJobState:
    def _write_events(self, tmp_events_dir, events):
        writer = EventWriter(events_dir=tmp_events_dir, flush_interval=9999)
        for ev in events:
            writer.append(ev)
        writer.flush()

    def test_rebuild_full_lifecycle(self, reader, tmp_events_dir):
        events = [
            _make_event(event_type=EventType.JOB_SUBMITTED, data={"user_input": "search"}),
            _make_event(event_type=EventType.JOB_STATUS_CHANGED, data={"new_status": "running"}),
            _make_event(event_type=EventType.PLAN_CREATED, data={"plan_summary": "2 tasks"}),
            _make_event(event_type=EventType.TASK_STARTED, data={"task_id": "t1", "task_type": "web_worker"}),
            _make_event(event_type=EventType.TASK_COMPLETED, data={"task_id": "t1", "result_preview": "ok"}),
            _make_event(event_type=EventType.TASK_STARTED, data={"task_id": "t2", "task_type": "file_worker"}),
            _make_event(event_type=EventType.TASK_FAILED, data={"task_id": "t2", "error": "timeout"}),
            _make_event(event_type=EventType.ARTIFACT_CREATED, data={"artifact_id": "a1", "path": "/tmp/x"}),
            _make_event(event_type=EventType.JOB_STATUS_CHANGED, data={"new_status": "completed", "intent": "search"}),
        ]
        self._write_events(tmp_events_dir, events)
        loaded = reader.load_session("sess_1")
        state = reader.rebuild_job_state(loaded, "job_1")
        assert state["status"] == "completed"
        assert state["intent"] == "search"
        assert state["plan"] == "2 tasks"
        assert len(state["tasks"]) == 2
        task_by_id = {t["task_id"]: t for t in state["tasks"]}
        assert task_by_id["t1"]["status"] == "completed"
        assert task_by_id["t2"]["status"] == "failed"
        assert len(state["artifacts"]) == 1

    def test_rebuild_empty(self, reader):
        state = reader.rebuild_job_state([], "job_x")
        assert state["status"] == "unknown"
        assert state["tasks"] == []


# ===========================================================================
# EventReader — conversation rebuild
# ===========================================================================

class TestRebuildConversation:
    def test_conversation_from_events(self, reader, tmp_events_dir):
        writer = EventWriter(events_dir=tmp_events_dir, flush_interval=9999)
        events = [
            _make_event(event_type=EventType.JOB_SUBMITTED, data={"user_input": "hello"}),
            _make_event(event_type=EventType.PLAN_CREATED, data={"plan_summary": "plan A"}),
            _make_event(event_type=EventType.TASK_COMPLETED, data={"task_id": "t1", "result_preview": "done"}),
        ]
        for ev in events:
            writer.append(ev)
        writer.flush()
        loaded = reader.load_session("sess_1")
        conv = reader.rebuild_conversation(loaded, "job_1")
        assert len(conv) == 3
        assert conv[0]["role"] == "user"
        assert conv[1]["role"] == "system"
        assert conv[2]["role"] == "assistant"


# ===========================================================================
# EventReader — metadata scan
# ===========================================================================

class TestMetadataScan:
    def test_scan_metadata(self, reader, tmp_events_dir):
        writer = EventWriter(events_dir=tmp_events_dir, flush_interval=9999)
        writer.append(SessionEvent(
            event_type=EventType.SESSION_START,
            session_id="sess_1",
            data={"source": "cli"},
        ))
        writer.append(_make_event(event_type=EventType.METADATA_UPDATED, data={"label": "test"}))
        writer.flush()
        meta = reader.scan_session_metadata("sess_1")
        assert meta["session_id"] == "sess_1"
        assert meta.get("source") == "cli"
        assert meta.get("label") == "test"

    def test_scan_nonexistent(self, reader):
        assert reader.scan_session_metadata("nope") == {}

    def test_list_sessions(self, reader, tmp_events_dir):
        writer = EventWriter(events_dir=tmp_events_dir, flush_interval=9999)
        writer.append(SessionEvent(
            event_type=EventType.SESSION_START, session_id="s1", data={"source": "cli"},
        ))
        writer.append(SessionEvent(
            event_type=EventType.SESSION_START, session_id="s2", data={"source": "ui"},
        ))
        writer.flush()
        sessions = reader.list_sessions()
        assert len(sessions) == 2
        ids = {s["session_id"] for s in sessions}
        assert ids == {"s1", "s2"}


# ===========================================================================
# Module-level helpers (emit_event / flush_events)
# ===========================================================================

class TestEmitEvent:
    def test_emit_disabled(self):
        with patch("core.event_log._is_enabled", return_value=False):
            result = emit_event(EventType.JOB_SUBMITTED, session_id="s1", job_id="j1")
            assert result is None

    def test_emit_enabled(self, tmp_events_dir):
        writer = EventWriter(events_dir=tmp_events_dir, flush_interval=9999)
        with patch("core.event_log._is_enabled", return_value=True), \
             patch("core.event_log.get_event_writer", return_value=writer):
            result = emit_event(
                EventType.JOB_SUBMITTED,
                session_id="s1", job_id="j1",
                data={"user_input": "test"},
            )
            assert result is not None
            assert result.event_type == EventType.JOB_SUBMITTED
            assert writer.pending_count == 1

    def test_emit_empty_session_returns_none(self):
        with patch("core.event_log._is_enabled", return_value=True):
            result = emit_event(EventType.JOB_SUBMITTED, session_id="", job_id="j1")
            assert result is None

    def test_flush_disabled(self):
        with patch("core.event_log._is_enabled", return_value=False):
            assert flush_events() == 0
