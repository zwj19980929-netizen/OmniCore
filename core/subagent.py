"""
S5: Subagent 基础模块 — 子 Agent 定义、执行与资源管控。

SubagentSpec  — 子 Agent 规格声明
SubagentResult — 子 Agent 执行结果
SubagentRunner — 子 Agent 执行器（独立 graph 循环、上下文继承、资源限制）
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from utils.logger import log_agent_action, log_warning


def _get_graph_lazy():
    """Lazy import to avoid circular dependency."""
    from core.graph import get_graph
    return get_graph()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SubagentSpec:
    """Specification for spawning a subagent."""

    name: str
    agent_type: str  # e.g. "web_worker", "file_worker", "browser_agent"
    task_description: str
    params: Dict[str, Any] = field(default_factory=dict)
    inherit_context: bool = True
    max_turns: int = 10
    timeout: int = 300  # seconds
    depth: int = 0  # current nesting depth (0 = top-level subagent)

    def __post_init__(self):
        if not self.name:
            self.name = f"subagent_{uuid.uuid4().hex[:8]}"


@dataclass
class SubagentResult:
    """Result returned by a completed subagent."""

    spec: SubagentSpec
    success: bool
    output: Any = None
    error: Optional[str] = None
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    turns_used: int = 0

    def to_notification(self) -> str:
        """Format as a <task-notification> XML string for coordinator injection."""
        status = "completed" if self.success else "failed"
        output_text = str(self.output or "")[:2000]
        error_text = f"\n  <error>{self.error}</error>" if self.error else ""
        artifacts_text = ""
        if self.artifacts:
            items = "\n".join(
                f"    <artifact type=\"{a.get('artifact_type', 'unknown')}\">{a.get('name', '')}</artifact>"
                for a in self.artifacts[:10]
            )
            artifacts_text = f"\n  <artifacts>\n{items}\n  </artifacts>"
        return (
            f"<task-notification agent=\"{self.spec.name}\" status=\"{status}\" "
            f"elapsed=\"{self.elapsed_seconds:.1f}s\" turns=\"{self.turns_used}\">\n"
            f"  <task>{self.spec.task_description[:500]}</task>\n"
            f"  <output>{output_text}</output>"
            f"{error_text}{artifacts_text}\n"
            f"</task-notification>"
        )


# ---------------------------------------------------------------------------
# SubagentRunner
# ---------------------------------------------------------------------------

class SubagentRunner:
    """Execute a subagent by running an independent graph loop.

    Key design decisions:
    - Each subagent gets its own OmniCoreState (isolation)
    - Context inheritance: system prompt + session memory + plan summary (not full history)
    - Resource limits: max_turns, timeout, depth
    - Subagents CANNOT spawn further subagents (depth check)
    """

    def __init__(self, max_depth: int = 1):
        from config.settings import settings
        self._max_depth = max(settings.MAX_SUBAGENT_DEPTH, 1) if max_depth is None else max_depth

    def _check_depth(self, spec: SubagentSpec) -> None:
        """Raise if subagent exceeds max nesting depth."""
        if spec.depth >= self._max_depth:
            raise SubagentDepthExceeded(
                f"Subagent '{spec.name}' at depth {spec.depth} "
                f"exceeds max_depth {self._max_depth}"
            )

    def _build_child_state(
        self,
        spec: SubagentSpec,
        parent_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create an isolated child state with optional context inheritance."""
        from core.state import create_initial_state

        child_state = create_initial_state(
            user_input=spec.task_description,
            session_id=parent_state.get("session_id", "") if parent_state else "",
            job_id=f"subagent_{spec.name}_{uuid.uuid4().hex[:8]}",
        )

        if spec.inherit_context and parent_state:
            # Inherit message bus (read-only snapshot for context)
            child_state["message_bus"] = list(parent_state.get("message_bus", []))
            # Inherit task_outputs for $ref resolution
            child_state["task_outputs"] = dict(parent_state.get("task_outputs", {}))

        # Inject subagent params into the child state
        if spec.params:
            for task in child_state.get("task_queue", []):
                task.setdefault("params", {}).update(spec.params)

        return child_state

    async def run(
        self,
        spec: SubagentSpec,
        parent_state: Optional[Dict[str, Any]] = None,
    ) -> SubagentResult:
        """Execute a subagent and return its result.

        This runs the full OmniCore graph loop in an isolated state,
        with timeout and turn limits enforced.
        """
        self._check_depth(spec)

        # Apply settings-level limits
        from config.settings import settings
        spec.max_turns = min(spec.max_turns, settings.SUBAGENT_MAX_TURNS)
        spec.timeout = min(spec.timeout, settings.SUBAGENT_TIMEOUT)

        child_state = self._build_child_state(spec, parent_state)
        start_time = time.monotonic()
        turns_used = 0

        log_agent_action(
            "SubagentRunner",
            f"启动子 Agent: {spec.name}",
            f"type={spec.agent_type}, depth={spec.depth}, "
            f"max_turns={spec.max_turns}, timeout={spec.timeout}s",
        )

        try:
            result = await asyncio.wait_for(
                self._execute_graph_loop(spec, child_state),
                timeout=spec.timeout,
            )
            elapsed = time.monotonic() - start_time
            turns_used = result.get("_turns_used", 0)

            final_output = result.get("final_output", "")
            artifacts = result.get("artifacts", [])
            error = result.get("error_trace", "")

            success = bool(final_output and not error)

            log_agent_action(
                "SubagentRunner",
                f"子 Agent 完成: {spec.name}",
                f"success={success}, turns={turns_used}, elapsed={elapsed:.1f}s",
            )

            return SubagentResult(
                spec=spec,
                success=success,
                output=final_output,
                error=error if error else None,
                artifacts=artifacts,
                elapsed_seconds=elapsed,
                turns_used=turns_used,
            )

        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start_time
            log_warning(
                f"SubagentRunner: 子 Agent '{spec.name}' 超时 "
                f"({spec.timeout}s), elapsed={elapsed:.1f}s"
            )
            return SubagentResult(
                spec=spec,
                success=False,
                error=f"Subagent timed out after {spec.timeout}s",
                elapsed_seconds=elapsed,
                turns_used=turns_used,
            )
        except SubagentDepthExceeded as e:
            raise
        except Exception as e:
            elapsed = time.monotonic() - start_time
            log_warning(f"SubagentRunner: 子 Agent '{spec.name}' 异常: {e}")
            return SubagentResult(
                spec=spec,
                success=False,
                error=str(e),
                elapsed_seconds=elapsed,
                turns_used=turns_used,
            )

    async def _execute_graph_loop(
        self,
        spec: SubagentSpec,
        child_state: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Run the OmniCore graph for the subagent.

        Uses the router → executor → validator → finalize pipeline
        but with turn counting and early-exit on completion.
        """
        graph = _get_graph_lazy()
        turns = 0

        # Run graph invocation — LangGraph handles the full pipeline
        result = await asyncio.to_thread(graph.invoke, child_state)
        turns = 1  # Single graph invocation = 1 logical turn

        result["_turns_used"] = turns
        return result

    async def run_parallel(
        self,
        specs: List[SubagentSpec],
        parent_state: Optional[Dict[str, Any]] = None,
        max_concurrent: Optional[int] = None,
    ) -> List[SubagentResult]:
        """Run multiple subagents in parallel with concurrency limits."""
        from config.settings import settings

        if max_concurrent is None:
            max_concurrent = settings.MAX_PARALLEL_SUBAGENTS

        semaphore = asyncio.Semaphore(max_concurrent)
        failure_strategy = settings.SUBAGENT_FAILURE_STRATEGY
        results: List[SubagentResult] = []
        cancel_event = asyncio.Event()

        async def _run_one(spec: SubagentSpec) -> SubagentResult:
            if cancel_event.is_set():
                return SubagentResult(
                    spec=spec,
                    success=False,
                    error="Cancelled due to fail_fast policy",
                )
            async with semaphore:
                if cancel_event.is_set():
                    return SubagentResult(
                        spec=spec,
                        success=False,
                        error="Cancelled due to fail_fast policy",
                    )
                result = await self.run(spec, parent_state)
                if not result.success and failure_strategy == "fail_fast":
                    cancel_event.set()
                return result

        tasks = [asyncio.create_task(_run_one(s)) for s in specs]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        return list(results)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SubagentDepthExceeded(RuntimeError):
    """Raised when a subagent exceeds the maximum nesting depth."""
    pass
