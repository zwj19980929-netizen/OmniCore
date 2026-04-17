"""
Unit tests for ``core.runtime.purge_session_working_memory`` (A4).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


class _TieredStub:
    def __init__(self, result: int = 3):
        self.calls: list[str] = []
        self.result = result

    def purge_working(self, session_id: str) -> int:
        self.calls.append(session_id)
        return self.result


class TestPurgeSessionWorkingMemory:
    def test_empty_session_id_returns_zero(self):
        from core.runtime import purge_session_working_memory
        assert purge_session_working_memory("") == 0
        assert purge_session_working_memory(None) == 0  # type: ignore[arg-type]

    def test_returns_count_from_tiered_store(self, monkeypatch):
        from core import runtime

        monkeypatch.setattr(runtime, "_resolve_runtime_memory", lambda: object())
        tiered = _TieredStub(result=5)

        class _MgrStub:
            def __init__(self, memory_store):
                self.memory_store = memory_store

            @property
            def tiered_store(self):
                return tiered

        monkeypatch.setattr(runtime, "MemoryManager", _MgrStub)
        assert runtime.purge_session_working_memory("sess-1") == 5
        assert tiered.calls == ["sess-1"]

    def test_returns_zero_when_tiered_disabled(self, monkeypatch):
        from core import runtime

        monkeypatch.setattr(runtime, "_resolve_runtime_memory", lambda: object())

        class _MgrStub:
            def __init__(self, memory_store):
                pass

            @property
            def tiered_store(self):
                return None

        monkeypatch.setattr(runtime, "MemoryManager", _MgrStub)
        assert runtime.purge_session_working_memory("sess-1") == 0

    def test_returns_zero_when_no_runtime_memory(self, monkeypatch):
        from core import runtime
        monkeypatch.setattr(runtime, "_resolve_runtime_memory", lambda: None)
        assert runtime.purge_session_working_memory("sess-1") == 0

    def test_swallows_exceptions(self, monkeypatch):
        from core import runtime

        monkeypatch.setattr(runtime, "_resolve_runtime_memory", lambda: object())

        class _Boom:
            def __init__(self, memory_store):
                raise RuntimeError("nope")

        monkeypatch.setattr(runtime, "MemoryManager", _Boom)
        assert runtime.purge_session_working_memory("sess-1") == 0
