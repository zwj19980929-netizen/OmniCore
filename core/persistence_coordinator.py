"""
Persistence Coordinator - ensures eventual consistency across storage layers.

Uses an outbox pattern: operations are first recorded in a local outbox,
then dispatched to each store. Failed operations are retried by a background
reconciliation loop.

The three independent persistence layers:
  1. RuntimeStateStore  (utils/runtime_state_store.py) - job/session/checkpoint/artifact
  2. WorkContextStore   (utils/work_context_store.py)  - goal/project/todo/experience
  3. ChromaMemory        (memory/scoped_chroma_store.py) - vector memories

This coordinator sits ON TOP and never modifies the underlying stores.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_OUTBOX_STATUSES = ("pending", "completed", "failed")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PersistenceOp:
    """A single persistence operation tracked in the outbox."""

    op_id: str
    op_type: str            # "complete_job", "persist_memory", "record_experience"
    store: str              # "runtime", "memory", "work_context"
    payload: Dict[str, Any]
    status: str = "pending"  # "pending" | "completed" | "failed"
    attempts: int = 0
    max_attempts: int = 3
    last_error: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PersistenceOp":
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class PersistenceCoordinator:
    """Coordinates writes across RuntimeStateStore, ChromaMemory, and WorkContextStore."""

    def __init__(
        self,
        runtime_store: Any = None,
        memory_store: Any = None,
        work_context_store: Any = None,
        outbox_path: Optional[Path] = None,
    ):
        # Stores are lazy-loaded on first use to avoid heavy imports at module level.
        self._runtime_store = runtime_store
        self._memory_store = memory_store
        self._work_context_store = work_context_store

        # Outbox persistence
        self._outbox_path = outbox_path or self._default_outbox_path()
        self._outbox: List[PersistenceOp] = []
        self._lock = threading.Lock()
        self._load_outbox()

    # -- lazy store access ---------------------------------------------------

    @property
    def runtime_store(self):
        if self._runtime_store is None:
            from utils.runtime_state_store import RuntimeStateStore
            self._runtime_store = RuntimeStateStore()
        return self._runtime_store

    @property
    def memory_store(self):
        if self._memory_store is None:
            from memory.scoped_chroma_store import ChromaMemory
            self._memory_store = ChromaMemory()
        return self._memory_store

    @property
    def work_context_store(self):
        if self._work_context_store is None:
            from utils.work_context_store import WorkContextStore
            self._work_context_store = WorkContextStore()
        return self._work_context_store

    # -- public API ----------------------------------------------------------

    async def complete_job(
        self,
        job_id: str,
        result: Dict[str, Any],
        scope: Dict[str, Any],
    ) -> Dict[str, bool]:
        """
        Atomically complete a job across all stores.

        *result* should carry the keys consumed by the individual stores:
            session_id, status, success, output, error, intent,
            tasks, policy_decisions, artifacts, is_special_command,
            user_input, tool_sequence, failure_reason, summary, ...

        Returns ``{store_name: success}`` for each store.
        Failed stores are queued for reconciliation.
        """
        session_id = str(result.get("session_id", "") or scope.get("session_id", "") or "")
        goal_id = str(scope.get("goal_id", "") or "")
        project_id = str(scope.get("project_id", "") or "")
        todo_id = str(scope.get("todo_id", "") or "")
        success_flag = bool(result.get("success", False))

        # Build sub-operations for each store
        group_id = uuid.uuid4().hex[:12]

        runtime_op = PersistenceOp(
            op_id=f"op_{group_id}_runtime",
            op_type="complete_job",
            store="runtime",
            payload={
                "session_id": session_id,
                "job_id": job_id,
                "status": str(result.get("status", "completed")),
                "success": success_flag,
                "output": str(result.get("output", "") or ""),
                "error": str(result.get("error", "") or ""),
                "intent": str(result.get("intent", "") or ""),
                "tasks": result.get("tasks") or [],
                "policy_decisions": result.get("policy_decisions") or [],
                "artifacts": result.get("artifacts") or [],
                "is_special_command": bool(result.get("is_special_command", False)),
            },
        )

        memory_op = PersistenceOp(
            op_id=f"op_{group_id}_memory",
            op_type="complete_job",
            store="memory",
            payload={
                "job_id": job_id,
                "user_input": str(result.get("user_input", "") or ""),
                "success": success_flag,
                "final_output": str(result.get("output", "") or ""),
                "final_error": str(result.get("error", "") or ""),
                "intent": str(result.get("intent", "") or ""),
                "scope": scope,
                "tasks": result.get("tasks") or [],
                "artifacts": result.get("artifacts") or [],
                "is_special_command": bool(result.get("is_special_command", False)),
            },
        )

        work_ctx_op = PersistenceOp(
            op_id=f"op_{group_id}_work_context",
            op_type="complete_job",
            store="work_context",
            payload={
                "job_id": job_id,
                "goal_id": goal_id,
                "project_id": project_id,
                "todo_id": todo_id,
                "success": success_flag,
            },
        )

        ops = [runtime_op, memory_op, work_ctx_op]
        with self._lock:
            for op in ops:
                self._outbox.append(op)
            self._save_outbox()

        # Dispatch concurrently
        results_map: Dict[str, bool] = {}
        dispatch = await asyncio.gather(
            self._execute_op(runtime_op),
            self._execute_op(memory_op),
            self._execute_op(work_ctx_op),
            return_exceptions=True,
        )
        for op, outcome in zip(ops, dispatch):
            if isinstance(outcome, BaseException):
                op.status = "failed"
                op.last_error = str(outcome)[:500]
                results_map[op.store] = False
                logger.warning(
                    "PersistenceCoordinator: %s/%s failed: %s",
                    op.op_type, op.store, op.last_error,
                )
            else:
                results_map[op.store] = bool(outcome)

        with self._lock:
            self._save_outbox()

        return results_map

    async def persist_memory(
        self,
        job_id: str,
        content: str,
        scope: Dict[str, Any],
        memory_type: str = "experience",
    ) -> bool:
        """Persist a memory entry with scope consistency."""
        op = PersistenceOp(
            op_id=f"op_{uuid.uuid4().hex[:12]}_memory",
            op_type="persist_memory",
            store="memory",
            payload={
                "job_id": job_id,
                "content": content,
                "scope": scope,
                "memory_type": memory_type,
            },
        )
        with self._lock:
            self._outbox.append(op)
            self._save_outbox()

        success = await self._execute_op(op)

        with self._lock:
            self._save_outbox()

        return success

    async def record_experience(
        self,
        job_id: str,
        tool_sequence: List[str],
        success: bool,
        failure_reason: str = "",
        scope: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Record execution experience in work context store."""
        scope = scope or {}
        op = PersistenceOp(
            op_id=f"op_{uuid.uuid4().hex[:12]}_work_context",
            op_type="record_experience",
            store="work_context",
            payload={
                "job_id": job_id,
                "session_id": str(scope.get("session_id", "") or ""),
                "goal_id": str(scope.get("goal_id", "") or ""),
                "project_id": str(scope.get("project_id", "") or ""),
                "todo_id": str(scope.get("todo_id", "") or ""),
                "tool_sequence": tool_sequence,
                "success": success,
                "failure_reason": failure_reason,
            },
        )
        with self._lock:
            self._outbox.append(op)
            self._save_outbox()

        result = await self._execute_op(op)

        with self._lock:
            self._save_outbox()

        return result

    async def reconcile(self) -> int:
        """
        Process failed/pending operations from the outbox.
        Returns count of successfully reconciled operations.
        Called by background worker or manually.
        """
        with self._lock:
            pending = [
                op for op in self._outbox
                if op.status in ("pending", "failed") and op.attempts < op.max_attempts
            ]

        if not pending:
            return 0

        reconciled = 0
        for op in pending:
            # Exponential backoff: wait 1s * 2^(attempts-1) before retry
            if op.attempts > 0:
                backoff = min(1.0 * (2 ** (op.attempts - 1)), 16.0)
                await asyncio.sleep(backoff)

            ok = await self._execute_op(op)
            if ok:
                reconciled += 1

        with self._lock:
            self._save_outbox()
            self._compact_outbox()

        return reconciled

    def get_outbox_status(self) -> Dict[str, Any]:
        """Return outbox metrics: pending, failed, completed counts."""
        with self._lock:
            counts = {"pending": 0, "failed": 0, "completed": 0}
            for op in self._outbox:
                if op.status in counts:
                    counts[op.status] += 1
            counts["total"] = len(self._outbox)
            return counts

    # -- internal ------------------------------------------------------------

    def _enqueue_op(self, op: PersistenceOp) -> None:
        """Add an operation to the outbox (caller must hold lock)."""
        self._outbox.append(op)
        self._save_outbox()

    async def _execute_op(self, op: PersistenceOp) -> bool:
        """
        Execute a single persistence operation.
        Updates op status/attempts in place. Returns True on success.
        """
        op.attempts += 1
        try:
            if op.op_type == "complete_job":
                await self._dispatch_complete_job(op)
            elif op.op_type == "persist_memory":
                await self._dispatch_persist_memory(op)
            elif op.op_type == "record_experience":
                await self._dispatch_record_experience(op)
            else:
                op.status = "failed"
                op.last_error = f"Unknown op_type: {op.op_type}"
                return False

            op.status = "completed"
            op.last_error = ""
            return True
        except Exception as exc:
            op.last_error = str(exc)[:500]
            if op.attempts >= op.max_attempts:
                op.status = "failed"
                logger.error(
                    "PersistenceCoordinator: op %s permanently failed after %d attempts: %s",
                    op.op_id, op.attempts, op.last_error,
                )
            else:
                op.status = "pending"
                logger.warning(
                    "PersistenceCoordinator: op %s attempt %d failed: %s",
                    op.op_id, op.attempts, op.last_error,
                )
            return False

    # -- dispatch helpers (sync store calls wrapped for async) ---------------

    async def _dispatch_complete_job(self, op: PersistenceOp) -> None:
        """Dispatch a complete_job operation to the appropriate store."""
        payload = op.payload
        store = op.store
        loop = asyncio.get_event_loop()

        if store == "runtime":
            await loop.run_in_executor(
                None,
                lambda: self.runtime_store.complete_job(
                    session_id=payload["session_id"],
                    job_id=payload["job_id"],
                    status=payload.get("status", "completed"),
                    success=payload.get("success", False),
                    output=payload.get("output", ""),
                    error=payload.get("error", ""),
                    intent=payload.get("intent", ""),
                    tasks=payload.get("tasks", []),
                    policy_decisions=payload.get("policy_decisions", []),
                    artifacts=payload.get("artifacts", []),
                    is_special_command=payload.get("is_special_command", False),
                ),
            )

        elif store == "memory":
            # Use MemoryManager.persist_job_outcome for rich memory persistence
            from memory.manager import MemoryManager
            mgr = MemoryManager(chroma_memory=self.memory_store)
            await loop.run_in_executor(
                None,
                lambda: mgr.persist_job_outcome(
                    user_input=payload.get("user_input", ""),
                    success=payload.get("success", False),
                    final_output=payload.get("final_output", ""),
                    final_error=payload.get("final_error", ""),
                    intent=payload.get("intent", ""),
                    scope=payload.get("scope"),
                    tasks=payload.get("tasks", []),
                    artifacts=payload.get("artifacts", []),
                    is_special_command=payload.get("is_special_command", False),
                ),
            )

        elif store == "work_context":
            await loop.run_in_executor(
                None,
                lambda: self.work_context_store.record_job_link(
                    job_id=payload["job_id"],
                    goal_id=payload.get("goal_id", ""),
                    project_id=payload.get("project_id", ""),
                    todo_id=payload.get("todo_id", ""),
                    success=payload.get("success", False),
                ),
            )
        else:
            raise ValueError(f"Unknown store: {store}")

    async def _dispatch_persist_memory(self, op: PersistenceOp) -> None:
        """Dispatch a persist_memory operation to ChromaMemory."""
        payload = op.payload
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self.memory_store.add_memory(
                content=payload["content"],
                metadata={"job_id": payload.get("job_id", "")},
                memory_type=payload.get("memory_type", "experience"),
                scope=payload.get("scope"),
                fingerprint=f"coord:{payload.get('job_id', '')}:{payload.get('memory_type', '')}",
                allow_update=True,
            ),
        )

    async def _dispatch_record_experience(self, op: PersistenceOp) -> None:
        """Dispatch a record_experience operation to WorkContextStore."""
        payload = op.payload
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self.work_context_store.record_experience(
                session_id=payload.get("session_id", ""),
                job_id=payload.get("job_id", ""),
                user_input=payload.get("user_input", ""),
                intent=payload.get("intent", ""),
                tool_sequence=payload.get("tool_sequence", []),
                success=payload.get("success", False),
                goal_id=payload.get("goal_id", ""),
                project_id=payload.get("project_id", ""),
                todo_id=payload.get("todo_id", ""),
                summary=payload.get("summary", ""),
                failure_reason=payload.get("failure_reason", ""),
            ),
        )

    # -- outbox persistence --------------------------------------------------

    @staticmethod
    def _default_outbox_path() -> Path:
        from config.settings import settings
        return settings.DATA_DIR / "persistence_outbox.jsonl"

    def _load_outbox(self) -> None:
        """Load pending/failed ops from the outbox file on disk."""
        if not self._outbox_path.exists():
            return
        loaded: List[PersistenceOp] = []
        try:
            with self._outbox_path.open("r", encoding="utf-8") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(data, dict):
                        continue
                    # Only reload ops that still need processing
                    if data.get("status") in ("pending", "failed"):
                        try:
                            loaded.append(PersistenceOp.from_dict(data))
                        except (TypeError, KeyError):
                            continue
        except OSError as exc:
            logger.warning("PersistenceCoordinator: failed to load outbox: %s", exc)
        self._outbox = loaded

    def _save_outbox(self) -> None:
        """
        Persist the current outbox to disk (append-only JSONL).
        Caller must hold ``self._lock`` or guarantee single-threaded access.
        """
        try:
            self._outbox_path.parent.mkdir(parents=True, exist_ok=True)
            with self._outbox_path.open("w", encoding="utf-8") as fh:
                for op in self._outbox:
                    fh.write(json.dumps(op.to_dict(), ensure_ascii=False, sort_keys=True))
                    fh.write("\n")
        except OSError as exc:
            logger.error("PersistenceCoordinator: failed to save outbox: %s", exc)

    def _compact_outbox(self) -> None:
        """
        Remove completed operations from the in-memory outbox and rewrite the file.
        Keeps failed ops (with attempts exhausted) for auditing up to a cap.
        Caller must hold ``self._lock``.
        """
        max_keep_failed = 200
        failed = [op for op in self._outbox if op.status == "failed"]
        active = [op for op in self._outbox if op.status in ("pending",)]
        # Keep the most recent failed ops for debugging
        self._outbox = active + failed[-max_keep_failed:]
        self._save_outbox()
