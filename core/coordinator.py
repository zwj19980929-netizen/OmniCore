"""
S5: Coordinator 模块 — 复杂任务拆分、子 Agent 并行调度、结果汇总。

CoordinatorNode 是主 graph 中的一个路由分支节点，不是独立 agent。
它复用现有 finalize / policy 逻辑。

流程：
1. LLM 分析任务 → 输出子任务拆分计划
2. 并行 dispatch SubagentRunner
3. 收集结果 → 注入 <task-notification> → LLM 合成最终答案
4. 写入 state["final_output"]
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.llm import LLMClient
from core.state import OmniCoreState
from core.subagent import SubagentSpec, SubagentResult, SubagentRunner
from core.message_bus import (
    MSG_COORDINATOR_DISPATCH,
    MSG_COORDINATOR_SYNTHESIS,
    MSG_SUBAGENT_COMPLETED,
    MSG_SUBAGENT_FAILED,
    MSG_SUBAGENT_STARTED,
)
from core.graph_utils import get_bus, save_bus
from utils.logger import log_agent_action, log_warning


_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_COORDINATOR_PROMPT_PATH = _PROMPTS_DIR / "coordinator_system.txt"

_coordinator_prompt_cache: Optional[str] = None


def _load_coordinator_prompt() -> str:
    """Load the coordinator system prompt (cached after first load)."""
    global _coordinator_prompt_cache
    if _coordinator_prompt_cache is not None:
        return _coordinator_prompt_cache
    try:
        _coordinator_prompt_cache = _COORDINATOR_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        _coordinator_prompt_cache = (
            "You are a coordinator. Decompose the task into subtasks. "
            "Output JSON with keys: analysis, subtasks (list of {name, agent_type, task_description, params}), "
            "synthesis_strategy."
        )
    return _coordinator_prompt_cache


# ---------------------------------------------------------------------------
# Task Decomposition (LLM call)
# ---------------------------------------------------------------------------

def _parse_decomposition(raw: str) -> Optional[Dict[str, Any]]:
    """Extract the decomposition JSON from LLM output."""
    # Try direct JSON parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Try extracting from markdown code block
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Try finding first { ... } block
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def decompose_task(user_input: str, context: str = "") -> Optional[Dict[str, Any]]:
    """Use LLM to decompose a complex task into subtasks.

    Returns a dict with keys: analysis, subtasks, synthesis_strategy.
    Returns None if decomposition fails or task is simple.
    """
    system_prompt = _load_coordinator_prompt()
    user_message = f"Decompose this task into parallel subtasks:\n\n{user_input}"
    if context:
        user_message += f"\n\nAdditional context:\n{context}"

    llm = LLMClient()
    response = llm.chat(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0.3,
        json_mode=True,
    )

    if not response or not response.content:
        return None

    result = _parse_decomposition(response.content)
    if not result or not result.get("subtasks"):
        return None

    return result


# ---------------------------------------------------------------------------
# Result Synthesis (LLM call)
# ---------------------------------------------------------------------------

def synthesize_results(
    user_input: str,
    results: List[SubagentResult],
    strategy: str = "summarize",
) -> str:
    """Synthesize subagent results into a final answer."""
    system_prompt = _load_coordinator_prompt()

    # Build task notification messages
    notifications = "\n\n".join(r.to_notification() for r in results)

    successful = [r for r in results if r.success]
    failed = [r for r in results if not r.success]

    status_summary = (
        f"{len(successful)} subagent(s) succeeded, {len(failed)} failed."
    )

    user_message = (
        f"## Original Task\n{user_input}\n\n"
        f"## Subagent Results ({status_summary})\n{notifications}\n\n"
        f"## Synthesis Strategy: {strategy}\n\n"
        f"Synthesize the above subagent results into a coherent final answer. "
        f"Output JSON with keys: synthesis, sources, confidence, notes."
    )

    llm = LLMClient()
    response = llm.chat(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0.3,
        json_mode=True,
    )

    if not response or not response.content:
        # Fallback: concatenate results
        return _fallback_synthesis(results)

    parsed = _parse_decomposition(response.content)
    if parsed and parsed.get("synthesis"):
        return parsed["synthesis"]

    # If LLM output isn't parseable JSON, use the raw text
    return response.content


def _fallback_synthesis(results: List[SubagentResult]) -> str:
    """Simple concatenation fallback when LLM synthesis fails."""
    parts = []
    for r in results:
        status = "completed" if r.success else "failed"
        parts.append(f"[{r.spec.name} - {status}]\n{r.output or r.error or 'No output'}")
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Coordinator Decision Logic
# ---------------------------------------------------------------------------

def is_coordinator_enabled() -> bool:
    """Check if coordinator feature is enabled in settings."""
    from config.settings import settings
    return settings.COORDINATOR_ENABLED


# ---------------------------------------------------------------------------
# Coordinator Node (graph node function)
# ---------------------------------------------------------------------------

def coordinator_node(state: OmniCoreState) -> OmniCoreState:
    """Main coordinator graph node.

    1. Decompose task via LLM
    2. Dispatch subagents in parallel
    3. Synthesize results
    4. Write final_output and mark execution complete
    """
    from config.settings import settings

    user_input = state.get("user_input", "")
    job_id = state.get("job_id", "")
    session_id = state.get("session_id", "")
    bus = get_bus(state)

    log_agent_action("Coordinator", "开始协调分解任务", user_input[:200])

    # Step 1: Decompose
    decomposition = decompose_task(user_input)

    if not decomposition or not decomposition.get("subtasks"):
        log_warning("Coordinator: 任务分解失败，降级为单 Agent 模式")
        state["execution_status"] = "routing"
        return _degrade_to_single_agent(state)

    subtasks = decomposition["subtasks"]
    strategy = decomposition.get("synthesis_strategy", "summarize")
    analysis = decomposition.get("analysis", "")

    log_agent_action(
        "Coordinator",
        f"任务分解完成: {len(subtasks)} 个子任务",
        f"strategy={strategy}, analysis={analysis[:200]}",
    )

    # Publish dispatch event
    bus.publish(
        source="coordinator",
        target="*",
        message_type=MSG_COORDINATOR_DISPATCH,
        payload={
            "subtask_count": len(subtasks),
            "strategy": strategy,
            "analysis": analysis,
            "subtask_names": [s.get("name", "") for s in subtasks],
        },
        job_id=job_id,
    )

    # Step 2: Build SubagentSpecs
    specs: List[SubagentSpec] = []
    for st in subtasks[:settings.MAX_PARALLEL_SUBAGENTS + 2]:  # slight over-provision, runner limits concurrency
        specs.append(SubagentSpec(
            name=st.get("name", ""),
            agent_type=st.get("agent_type", "web_worker"),
            task_description=st.get("task_description", ""),
            params=st.get("params", {}),
            inherit_context=True,
            max_turns=settings.SUBAGENT_MAX_TURNS,
            timeout=settings.SUBAGENT_TIMEOUT,
            depth=0,
        ))

    # Publish started events
    for spec in specs:
        bus.publish(
            source="coordinator",
            target=spec.name,
            message_type=MSG_SUBAGENT_STARTED,
            payload={"agent_type": spec.agent_type, "task": spec.task_description[:300]},
            job_id=job_id,
        )

    # Step 3: Dispatch subagents
    runner = SubagentRunner(max_depth=settings.MAX_SUBAGENT_DEPTH)
    results = _run_subagents_sync(runner, specs, state)

    # Publish completion events
    for result in results:
        msg_type = MSG_SUBAGENT_COMPLETED if result.success else MSG_SUBAGENT_FAILED
        bus.publish(
            source=result.spec.name,
            target="coordinator",
            message_type=msg_type,
            payload={
                "success": result.success,
                "output_preview": str(result.output or "")[:500],
                "error": result.error,
                "elapsed": result.elapsed_seconds,
            },
            job_id=job_id,
        )

    # Collect artifacts from subagents
    all_artifacts = []
    for result in results:
        all_artifacts.extend(result.artifacts)
    if all_artifacts:
        state["artifacts"] = list(state.get("artifacts", [])) + all_artifacts

    # Step 4: Synthesize
    successful = [r for r in results if r.success]
    failed = [r for r in results if not r.success]

    log_agent_action(
        "Coordinator",
        f"子 Agent 执行完毕: {len(successful)} 成功, {len(failed)} 失败",
        "",
    )

    if not successful:
        # All failed — degrade
        log_warning("Coordinator: 所有子 Agent 失败，降级为单 Agent 模式")
        state["error_trace"] = (
            f"Coordinator: All {len(failed)} subagents failed. "
            f"Errors: {'; '.join(r.error or 'unknown' for r in failed)}"
        )
        save_bus(state, bus)
        return _degrade_to_single_agent(state)

    final_output = synthesize_results(user_input, results, strategy)

    # Publish synthesis event
    bus.publish(
        source="coordinator",
        target="*",
        message_type=MSG_COORDINATOR_SYNTHESIS,
        payload={
            "strategy": strategy,
            "successful_count": len(successful),
            "failed_count": len(failed),
            "output_preview": final_output[:500],
        },
        job_id=job_id,
    )

    state["final_output"] = final_output
    state["execution_status"] = "completed"
    state["validator_passed"] = True
    state["critic_approved"] = True

    save_bus(state, bus)

    log_agent_action("Coordinator", "协调完成，最终结果已合成", f"output_len={len(final_output)}")
    return state


def _run_subagents_sync(
    runner: SubagentRunner,
    specs: List[SubagentSpec],
    parent_state: Dict[str, Any],
) -> List[SubagentResult]:
    """Run subagents, bridging async to sync for graph node compatibility."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We're inside an existing event loop — use a new thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                asyncio.run,
                runner.run_parallel(specs, parent_state),
            )
            return future.result()
    else:
        return asyncio.run(runner.run_parallel(specs, parent_state))


def _degrade_to_single_agent(state: OmniCoreState) -> OmniCoreState:
    """Degrade coordinator to single-agent mode.

    Clears the coordinator signal and lets the normal router → executor
    pipeline handle the task. The route_node will be re-invoked by the
    graph's conditional edges.
    """
    # Reset so that the graph routes to normal pipeline
    state["task_queue"] = []
    state["execution_status"] = "routing"
    return state
