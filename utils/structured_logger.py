"""
OmniCore Structured Logging Infrastructure

Provides JSONL file logging with daily rotation, context propagation via
contextvars, and an in-memory metrics aggregator for LLM calls, browser
actions, job outcomes, and replan events.
"""

import json
import logging
import os
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Context variables (async-safe, thread-safe via contextvars)
# ---------------------------------------------------------------------------
_ctx_job_id: ContextVar[Optional[str]] = ContextVar("job_id", default=None)
_ctx_stage: ContextVar[Optional[str]] = ContextVar("stage", default=None)
_ctx_agent: ContextVar[Optional[str]] = ContextVar("agent", default=None)
_ctx_step_no: ContextVar[Optional[int]] = ContextVar("step_no", default=None)

# ---------------------------------------------------------------------------
# Log directory
# ---------------------------------------------------------------------------
_LOG_DIR = Path(__file__).resolve().parent.parent / "data" / "logs"


def _ensure_log_dir() -> Path:
    """Create the log directory if it does not exist and return its path."""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    return _LOG_DIR


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------
class _JSONFormatter(logging.Formatter):
    """Formats each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        entry: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "job_id": getattr(record, "job_id", None),
            "stage": getattr(record, "stage", None),
            "agent": getattr(record, "agent", None),
            "step_no": getattr(record, "step_no", None),
            "event": getattr(record, "event", None),
            "detail": getattr(record, "detail", None),
            "duration_ms": getattr(record, "duration_ms", None),
        }
        # Merge any extra fields attached by callers
        extra = getattr(record, "extra_fields", None)
        if extra and isinstance(extra, dict):
            entry.update(extra)
        return json.dumps(entry, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# In-memory metrics aggregator
# ---------------------------------------------------------------------------
class _MetricsAggregator:
    """Thread-safe in-memory metrics store."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # LLM metrics keyed by model
        self.llm_calls: Dict[str, int] = defaultdict(int)
        self.llm_latency_ms: Dict[str, float] = defaultdict(float)
        self.llm_tokens_in: Dict[str, int] = defaultdict(int)
        self.llm_tokens_out: Dict[str, int] = defaultdict(int)
        # Browser action metrics keyed by action_type
        self.browser_attempts: Dict[str, int] = defaultdict(int)
        self.browser_successes: Dict[str, int] = defaultdict(int)
        # Job metrics
        self.jobs_total: int = 0
        self.jobs_success: int = 0
        self.jobs_duration_ms: float = 0.0
        # Replan
        self.replans: int = 0
        self.plans_total: int = 0

    # -- recording helpers --------------------------------------------------

    def record_llm_call(
        self, model: str, tokens_in: int, tokens_out: int, duration_ms: float
    ) -> None:
        with self._lock:
            self.llm_calls[model] += 1
            self.llm_latency_ms[model] += duration_ms
            self.llm_tokens_in[model] += tokens_in
            self.llm_tokens_out[model] += tokens_out

    def record_browser_action(self, action_type: str, success: bool) -> None:
        with self._lock:
            self.browser_attempts[action_type] += 1
            if success:
                self.browser_successes[action_type] += 1

    def record_job(self, success: bool, duration_ms: float) -> None:
        with self._lock:
            self.jobs_total += 1
            if success:
                self.jobs_success += 1
            self.jobs_duration_ms += duration_ms

    def record_replan(self) -> None:
        with self._lock:
            self.replans += 1

    def record_plan(self) -> None:
        with self._lock:
            self.plans_total += 1

    # -- snapshot -----------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """Return a point-in-time copy of all aggregated metrics."""
        with self._lock:
            llm_models = sorted(
                set(self.llm_calls.keys())
                | set(self.llm_latency_ms.keys())
            )
            llm = {}
            for m in llm_models:
                calls = self.llm_calls[m]
                llm[m] = {
                    "calls": calls,
                    "total_latency_ms": round(self.llm_latency_ms[m], 2),
                    "avg_latency_ms": (
                        round(self.llm_latency_ms[m] / calls, 2)
                        if calls
                        else 0.0
                    ),
                    "tokens_in": self.llm_tokens_in[m],
                    "tokens_out": self.llm_tokens_out[m],
                }

            action_types = sorted(
                set(self.browser_attempts.keys())
                | set(self.browser_successes.keys())
            )
            browser = {}
            for a in action_types:
                attempts = self.browser_attempts[a]
                successes = self.browser_successes[a]
                browser[a] = {
                    "attempts": attempts,
                    "successes": successes,
                    "success_rate": (
                        round(successes / attempts, 4) if attempts else 0.0
                    ),
                }

            avg_job_ms = (
                round(self.jobs_duration_ms / self.jobs_total, 2)
                if self.jobs_total
                else 0.0
            )

            return {
                "llm": llm,
                "browser_actions": browser,
                "jobs": {
                    "total": self.jobs_total,
                    "success": self.jobs_success,
                    "success_rate": (
                        round(self.jobs_success / self.jobs_total, 4)
                        if self.jobs_total
                        else 0.0
                    ),
                    "avg_duration_ms": avg_job_ms,
                },
                "replan": {
                    "replans": self.replans,
                    "plans_total": self.plans_total,
                    "replan_ratio": (
                        round(self.replans / self.plans_total, 4)
                        if self.plans_total
                        else 0.0
                    ),
                },
            }


# ---------------------------------------------------------------------------
# StructuredLogger (singleton)
# ---------------------------------------------------------------------------
class StructuredLogger:
    """Central structured logger: JSONL file output + in-memory metrics."""

    _instance: Optional["StructuredLogger"] = None
    _init_lock = threading.Lock()

    def __new__(cls) -> "StructuredLogger":
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._initialized = False
                    cls._instance = inst
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True

        self.metrics = _MetricsAggregator()

        # Set up a dedicated stdlib logger for JSONL output
        log_dir = _ensure_log_dir()
        log_file = log_dir / "omnicore.log"

        self._logger = logging.getLogger("omnicore.structured")
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False  # don't bubble to root/Rich handler

        # Daily rotation, keep 7 days
        handler = TimedRotatingFileHandler(
            filename=str(log_file),
            when="midnight",
            interval=1,
            backupCount=7,
            encoding="utf-8",
            utc=True,
        )
        handler.setFormatter(_JSONFormatter())
        handler.suffix = "%Y-%m-%d"
        self._logger.addHandler(handler)

    # -- internal emit ------------------------------------------------------

    def _emit(
        self,
        level: int,
        event: str,
        detail: Optional[str] = None,
        duration_ms: Optional[float] = None,
        **extra: Any,
    ) -> None:
        record = self._logger.makeRecord(
            name=self._logger.name,
            level=level,
            fn="",
            lno=0,
            msg="",
            args=(),
            exc_info=None,
        )
        record.job_id = _ctx_job_id.get()
        record.stage = _ctx_stage.get()
        record.agent = _ctx_agent.get()
        record.step_no = _ctx_step_no.get()
        record.event = event
        record.detail = detail
        record.duration_ms = duration_ms
        record.extra_fields = extra if extra else None
        self._logger.handle(record)

    # -- public convenience methods -----------------------------------------

    def log_event(
        self,
        event: str,
        detail: Optional[str] = None,
        level: int = logging.INFO,
        **extra: Any,
    ) -> None:
        """Log a generic event."""
        self._emit(level, event, detail, **extra)

    def log_llm_call(
        self,
        model: str,
        tokens_in: int,
        tokens_out: int,
        duration_ms: float,
    ) -> None:
        """Log an LLM API call and update metrics."""
        self._emit(
            logging.INFO,
            "llm_call",
            detail=model,
            duration_ms=round(duration_ms, 2),
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        self.metrics.record_llm_call(model, tokens_in, tokens_out, duration_ms)

    def log_action(
        self,
        action_type: str,
        target: str,
        confidence: float,
        result: str,
    ) -> None:
        """Log a browser/tool action and update metrics."""
        success = result.lower() in ("success", "ok", "done", "true")
        self._emit(
            logging.INFO,
            "action",
            detail=f"{action_type} -> {target}",
            action_type=action_type,
            target=target,
            confidence=confidence,
            result=result,
        )
        self.metrics.record_browser_action(action_type, success)

    def log_job(self, success: bool, duration_ms: float) -> None:
        """Record a completed job for metrics tracking."""
        self._emit(
            logging.INFO,
            "job_complete",
            detail="success" if success else "failure",
            duration_ms=round(duration_ms, 2),
        )
        self.metrics.record_job(success, duration_ms)

    def log_replan(self, reason: Optional[str] = None) -> None:
        """Record a replan event."""
        self._emit(logging.WARNING, "replan", detail=reason)
        self.metrics.record_replan()

    def log_plan(self) -> None:
        """Record a new plan creation."""
        self._emit(logging.INFO, "plan_created")
        self.metrics.record_plan()

    def get_metrics_snapshot(self) -> Dict[str, Any]:
        """Return current aggregated metrics dict."""
        return self.metrics.snapshot()


# ---------------------------------------------------------------------------
# LogContext context manager
# ---------------------------------------------------------------------------
@contextmanager
def LogContext(
    job_id: Optional[str] = None,
    stage: Optional[str] = None,
    agent: Optional[str] = None,
    step_no: Optional[int] = None,
):
    """Set contextual fields for all structured log calls within scope.

    Works correctly across both sync and async code thanks to contextvars.

    Usage::

        with LogContext(job_id="j-123", stage="browse", agent="browser_agent"):
            slog.log_event("page_loaded", detail="https://example.com")
    """
    tokens = []
    if job_id is not None:
        tokens.append(_ctx_job_id.set(job_id))
    if stage is not None:
        tokens.append(_ctx_stage.set(stage))
    if agent is not None:
        tokens.append(_ctx_agent.set(agent))
    if step_no is not None:
        tokens.append(_ctx_step_no.set(step_no))
    try:
        yield
    finally:
        for tok in tokens:
            # ContextVar.reset() restores the previous value
            tok.var.reset(tok)


# ---------------------------------------------------------------------------
# Module-level convenience singleton
# ---------------------------------------------------------------------------
def get_structured_logger() -> StructuredLogger:
    """Return the singleton StructuredLogger instance."""
    return StructuredLogger()
