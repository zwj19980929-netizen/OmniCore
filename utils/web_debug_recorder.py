"""
Generic, toggleable recorder for webpage perception debugging.
"""
from __future__ import annotations

import contextvars
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from config.settings import settings
from utils.logger import log_warning

_ACTIVE_TRACE: contextvars.ContextVar[Optional["WebDebugTrace"]] = contextvars.ContextVar(
    "web_perception_debug_trace",
    default=None,
)
_LAST_TRACE: Optional["WebDebugTrace"] = None


def _slugify(value: str, default: str = "trace") -> str:
    raw = str(value or "").strip().lower()
    raw = re.sub(r"[^a-z0-9._-]+", "-", raw)
    raw = raw.strip("._-")
    return raw[:48] or default


def _short_text(value: Any, limit: int = 240) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


@dataclass
class WebDebugTrace:
    trace_id: str
    scope: str
    root_dir: Path
    metadata: Dict[str, Any] = field(default_factory=dict)
    _sequence: int = 0

    def __post_init__(self) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.write_json(
            "manifest",
            {
                "trace_id": self.trace_id,
                "scope": self.scope,
                "created_at": datetime.utcnow().isoformat() + "Z",
                "metadata": self.metadata,
            },
        )

    def _next_prefix(self) -> str:
        self._sequence += 1
        return f"{self._sequence:03d}"

    def _path_for(self, label: str, suffix: str) -> Path:
        safe_label = _slugify(label, default="trace")
        safe_suffix = suffix if suffix.startswith(".") else f".{suffix}"
        return self.root_dir / f"{self._next_prefix()}_{safe_label}{safe_suffix}"

    def write_text(self, label: str, content: Any, suffix: str = ".txt") -> Optional[Path]:
        try:
            path = self._path_for(label, suffix)
            path.write_text(str(content or ""), encoding="utf-8")
            return path
        except Exception as exc:
            log_warning(f"web debug write_text failed [{label}]: {_short_text(exc)}")
            return None

    def write_json(self, label: str, payload: Any, suffix: str = ".json") -> Optional[Path]:
        try:
            path = self._path_for(label, suffix)
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            return path
        except Exception as exc:
            log_warning(f"web debug write_json failed [{label}]: {_short_text(exc)}")
            return None

    def write_binary(self, label: str, payload: bytes, suffix: str) -> Optional[Path]:
        try:
            path = self._path_for(label, suffix)
            path.write_bytes(payload or b"")
            return path
        except Exception as exc:
            log_warning(f"web debug write_binary failed [{label}]: {_short_text(exc)}")
            return None

    def record_event(self, label: str, **details: Any) -> Optional[Path]:
        return self.write_json(label, details)


def is_enabled() -> bool:
    return bool(settings.WEB_PERCEPTION_DEBUG)


def start_trace(scope: str, metadata: Optional[Dict[str, Any]] = None) -> Optional[WebDebugTrace]:
    if not is_enabled():
        return None
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    trace_id = f"{timestamp}_{_slugify(scope)}_{uuid4().hex[:8]}"
    root_dir = settings.WEB_PERCEPTION_DEBUG_DIR / trace_id
    return WebDebugTrace(
        trace_id=trace_id,
        scope=scope,
        root_dir=root_dir,
        metadata=dict(metadata or {}),
    )


def activate_trace(trace: Optional[WebDebugTrace]):
    global _LAST_TRACE
    if trace is not None:
        _LAST_TRACE = trace
    return _ACTIVE_TRACE.set(trace)


def deactivate_trace(token) -> None:
    try:
        _ACTIVE_TRACE.reset(token)
    except Exception:
        pass


def current_trace() -> Optional[WebDebugTrace]:
    trace = _ACTIVE_TRACE.get()
    if trace is not None:
        return trace
    return _LAST_TRACE if is_enabled() else None


def write_text(label: str, content: Any, suffix: str = ".txt") -> Optional[Path]:
    trace = current_trace()
    if not trace:
        return None
    return trace.write_text(label, content, suffix=suffix)


def write_json(label: str, payload: Any, suffix: str = ".json") -> Optional[Path]:
    trace = current_trace()
    if not trace:
        return None
    return trace.write_json(label, payload, suffix=suffix)


def write_binary(label: str, payload: bytes, suffix: str) -> Optional[Path]:
    trace = current_trace()
    if not trace:
        return None
    return trace.write_binary(label, payload, suffix=suffix)


def record_event(label: str, **details: Any) -> Optional[Path]:
    trace = current_trace()
    if not trace:
        return None
    return trace.record_event(label, **details)
