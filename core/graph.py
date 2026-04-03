"""
OmniCore LangGraph DAG — pure graph definition.

All node implementations live in dedicated modules:
  - core.graph_nodes     — route, plan_validator, executor, critic, validator, human_confirm
  - core.replanner       — replanner_node
  - core.finalizer       — finalize_node
  - core.graph_conditions — conditional routing functions
  - core.graph_utils     — shared helpers (bus, checkpoint, adaptive skip, etc.)

This file only declares nodes, wires edges, and exposes the compiled graph singleton.
"""

from langgraph.graph import StateGraph, END

from core.state import OmniCoreState
from core.stage_registry import StageRegistry

# -- Node functions --------------------------------------------------------
from core.graph_nodes import (                          # noqa: F401
    route_node,
    plan_validator_node,
    parallel_executor_node,
    dynamic_replan_node,
    critic_node,
    validator_node,
    human_confirm_node,
    human_confirm_node_v2,
    coordinator_node,
)
from core.replanner import replanner_node               # noqa: F401
from core.finalizer import finalize_node                # noqa: F401

# -- Condition functions ---------------------------------------------------
from core.graph_conditions import (
    should_continue_after_route,
    get_first_executor,
    after_parallel_executor,
    after_parallel_executor_adaptive,
    after_dynamic_replan,
    after_validator,
    should_retry_or_finish,
    MAX_REPLAN,
)

# -- Re-exports for backward compatibility --------------------------------
# Some tests and modules import helpers directly from core.graph.
from core.graph_utils import (                          # noqa: F401
    get_bus as _get_bus,
    save_bus as _save_bus,
    bus_get_str as _bus_get_str,
    bus_get as _bus_get,
    should_skip_for_resume as _should_skip_for_resume,
    save_runtime_checkpoint as _save_runtime_checkpoint,
    should_skip_remaining_tasks,
    apply_adaptive_skip as _apply_adaptive_skip,
    derive_authoritative_target_url as _derive_authoritative_target_url,
    repair_replan_task_params as _repair_replan_task_params,
    has_waiting_tasks as _has_waiting_tasks,
    mark_confirmation_required_tasks_waiting as _mark_confirmation_required_tasks_waiting,
)
from utils.text_repair import (                         # noqa: F401
    repair_mojibake_text as _repair_mojibake_text,
    normalize_text_value as _normalize_text_value,
    normalize_payload as _normalize_payload,
)
from utils.structured_extract import (                  # noqa: F401
    extract_structured_findings as _extract_structured_findings,
    extract_requested_item_count as _extract_requested_item_count,
)

from utils.logger import log_agent_action, log_warning


# =========================================================================
# Legacy hardcoded graph
# =========================================================================

def build_graph() -> StateGraph:
    """Build the hardcoded OmniCore execution graph (v0.2 topology)."""

    graph = StateGraph(OmniCoreState)

    # Nodes (9)
    graph.add_node("router", route_node)
    graph.add_node("coordinator", coordinator_node)  # S5
    graph.add_node("plan_validator", plan_validator_node)
    graph.add_node("human_confirm", human_confirm_node_v2)
    graph.add_node("parallel_executor", parallel_executor_node)
    graph.add_node("validator", validator_node)
    graph.add_node("replanner", replanner_node)
    graph.add_node("critic", critic_node)
    graph.add_node("finalize", finalize_node)

    # Entry
    graph.set_entry_point("router")

    # Router → coordinator | plan_validator | finalize (S5: added coordinator)
    graph.add_conditional_edges("router", should_continue_after_route, {
        "coordinator": "coordinator",
        "plan_validator": "plan_validator",
        "finalize": "finalize",
    })

    # Coordinator → finalize (coordinator handles full lifecycle)
    graph.add_edge("coordinator", "finalize")

    # plan_validator → human_confirm
    graph.add_edge("plan_validator", "human_confirm")

    # human_confirm → executor | validator | END
    graph.add_conditional_edges("human_confirm", get_first_executor, {
        "parallel_executor": "parallel_executor",
        "validator": "validator",
        "end": END,
    })

    # executor → self-loop | dynamic_replan | validator
    graph.add_conditional_edges("parallel_executor", after_parallel_executor, {
        "parallel_executor": "parallel_executor",
        "dynamic_replan": "dynamic_replan",
        "validator": "validator",
    })

    # dynamic_replan → executor | validator
    graph.add_conditional_edges("dynamic_replan", after_dynamic_replan, {
        "parallel_executor": "parallel_executor",
        "validator": "validator",
    })

    # validator → critic | replanner | finalize
    graph.add_conditional_edges("validator", after_validator, {
        "critic": "critic",
        "replanner": "replanner",
        "finalize": "finalize",
    })

    # replanner → executor | validator | END
    graph.add_conditional_edges("replanner", get_first_executor, {
        "parallel_executor": "parallel_executor",
        "validator": "validator",
        "end": END,
    })

    # critic → finalize | replanner
    graph.add_conditional_edges("critic", should_retry_or_finish, {
        "finalize": "finalize",
        "replanner": "replanner",
    })

    # finalize → END
    graph.add_edge("finalize", END)

    return graph


def compile_graph():
    """Compile the legacy hardcoded graph (backward compat)."""
    return build_graph().compile()


# =========================================================================
# Dynamic registry-driven graph (Direction 1)
# =========================================================================

def build_graph_from_registry(registry: StageRegistry = None):
    """Build the LangGraph DAG dynamically from the StageRegistry."""
    if registry is None:
        registry = StageRegistry.get_instance()

    graph = StateGraph(OmniCoreState)
    stages = registry.get_ordered_stages()
    stage_names = {s.name for s in stages}

    if not stages:
        raise RuntimeError("No stages registered in the StageRegistry")

    for stage in stages:
        graph.add_node(stage.name, stage.node_fn)

    graph.set_entry_point(stages[0].name)

    # Router → coordinator | plan_validator | finalize (S5: added coordinator)
    if "router" in stage_names:
        targets = {}
        if "coordinator" in stage_names:
            targets["coordinator"] = "coordinator"
        if "plan_validator" in stage_names:
            targets["plan_validator"] = "plan_validator"
        if "finalize" in stage_names:
            targets["finalize"] = "finalize"
        if targets:
            graph.add_conditional_edges("router", should_continue_after_route, targets)

    # Coordinator → finalize (S5)
    if "coordinator" in stage_names and "finalize" in stage_names:
        graph.add_edge("coordinator", "finalize")

    # plan_validator → human_confirm
    if "plan_validator" in stage_names and "human_confirm" in stage_names:
        graph.add_edge("plan_validator", "human_confirm")

    # human_confirm → executor | validator | END
    if "human_confirm" in stage_names:
        targets = {}
        if "parallel_executor" in stage_names:
            targets["parallel_executor"] = "parallel_executor"
        if "validator" in stage_names:
            targets["validator"] = "validator"
        targets["end"] = END
        graph.add_conditional_edges("human_confirm", get_first_executor, targets)

    # executor → self-loop | dynamic_replan | validator (with adaptive skip)
    if "parallel_executor" in stage_names:
        targets = {"parallel_executor": "parallel_executor"}
        if "dynamic_replan" in stage_names:
            targets["dynamic_replan"] = "dynamic_replan"
        if "validator" in stage_names:
            targets["validator"] = "validator"
        graph.add_conditional_edges("parallel_executor", after_parallel_executor_adaptive, targets)

    # dynamic_replan → executor | validator
    if "dynamic_replan" in stage_names:
        targets = {}
        if "parallel_executor" in stage_names:
            targets["parallel_executor"] = "parallel_executor"
        if "validator" in stage_names:
            targets["validator"] = "validator"
        if targets:
            graph.add_conditional_edges("dynamic_replan", after_dynamic_replan, targets)

    # validator → critic | replanner | finalize
    if "validator" in stage_names:
        targets = {}
        if "critic" in stage_names:
            targets["critic"] = "critic"
        if "replanner" in stage_names:
            targets["replanner"] = "replanner"
        if "finalize" in stage_names:
            targets["finalize"] = "finalize"
        if targets:
            graph.add_conditional_edges("validator", after_validator, targets)

    # replanner → executor | validator | END
    if "replanner" in stage_names:
        targets = {}
        if "parallel_executor" in stage_names:
            targets["parallel_executor"] = "parallel_executor"
        if "validator" in stage_names:
            targets["validator"] = "validator"
        targets["end"] = END
        graph.add_conditional_edges("replanner", get_first_executor, targets)

    # critic → finalize | replanner
    if "critic" in stage_names:
        targets = {}
        if "finalize" in stage_names:
            targets["finalize"] = "finalize"
        if "replanner" in stage_names:
            targets["replanner"] = "replanner"
        if targets:
            graph.add_conditional_edges("critic", should_retry_or_finish, targets)

    # finalize → END
    if "finalize" in stage_names:
        graph.add_edge("finalize", END)

    return graph.compile()


# =========================================================================
# Graph singleton
# =========================================================================

_USE_REGISTRY_GRAPH = True

omnicore_graph = None


def get_graph(use_registry: bool = None):
    """Return the compiled graph singleton."""
    global omnicore_graph
    if omnicore_graph is not None:
        return omnicore_graph

    should_use_registry = use_registry if use_registry is not None else _USE_REGISTRY_GRAPH

    if should_use_registry:
        try:
            omnicore_graph = build_graph_from_registry()
            log_agent_action(
                "GraphBuilder",
                "Built graph from StageRegistry",
                f"{len(StageRegistry.get_instance().list_names())} stages",
            )
        except Exception as exc:
            log_warning(f"Registry graph build failed, falling back to legacy: {exc}")
            omnicore_graph = compile_graph()
    else:
        omnicore_graph = compile_graph()

    return omnicore_graph
