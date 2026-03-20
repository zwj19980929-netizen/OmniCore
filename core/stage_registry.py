"""
Stage Registry - declarative graph stage management.

Each stage in the execution pipeline registers itself here.
The graph builder reads this registry to construct the LangGraph DAG dynamically,
replacing the manually wired ``build_graph()`` function over time.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class StageDefinition:
    """Metadata and callable for a single graph pipeline stage."""

    name: str                                   # e.g. "router", "executor", "critic"
    node_fn: Callable                           # The actual graph node function
    description: str
    order: int                                  # Default ordering (10, 20, 30, ...)
    required: bool = True                       # Must always run?
    depends_on: Tuple[str, ...] = ()            # Stage dependencies
    skip_condition: Optional[str] = None        # Condition expression evaluated against state


class StageRegistry:
    """Registry for graph pipeline stages.

    Stages are registered either programmatically or via the
    :func:`register_stage` decorator.  The registry maintains ordering and
    dependency metadata so that ``build_execution_plan`` can produce a valid
    topological execution order at runtime.
    """

    _instance: ClassVar[Optional[StageRegistry]] = None

    def __init__(self) -> None:
        self._stages: Dict[str, StageDefinition] = {}

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> StageRegistry:
        """Return the process-wide singleton registry."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton (useful for testing)."""
        cls._instance = None

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, stage: StageDefinition) -> None:
        """Register a stage definition, overwriting any previous entry with the same name."""
        if stage.name in self._stages:
            logger.warning("Overwriting existing stage '%s'", stage.name)
        self._stages[stage.name] = stage
        logger.info(
            "Registered stage '%s' (order=%d, required=%s)",
            stage.name, stage.order, stage.required,
        )

    def unregister(self, name: str) -> bool:
        """Remove a stage. Returns ``True`` if it existed."""
        removed = self._stages.pop(name, None)
        if removed is not None:
            logger.info("Unregistered stage '%s'", name)
        return removed is not None

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, name: str) -> Optional[StageDefinition]:
        """Return a stage by name, or ``None``."""
        return self._stages.get(name)

    def get_ordered_stages(self) -> List[StageDefinition]:
        """Return all stages sorted by their ``order`` field."""
        return sorted(self._stages.values(), key=lambda s: s.order)

    def list_names(self) -> List[str]:
        """Return registered stage names in order."""
        return [s.name for s in self.get_ordered_stages()]

    # ------------------------------------------------------------------
    # Execution planning
    # ------------------------------------------------------------------

    def build_execution_plan(self, state: Dict[str, Any]) -> List[str]:
        """Determine which stages should run and in what order.

        For each registered stage (sorted by ``order``):

        1. If ``skip_condition`` is set, evaluate it against *state*.
           When the expression is truthy the stage is skipped (unless it is
           ``required``).
        2. Verify that all ``depends_on`` stages are already in the plan.

        Returns an ordered list of stage names.
        """
        plan: List[str] = []
        plan_set: set[str] = set()

        for stage in self.get_ordered_stages():
            # Evaluate skip condition
            if stage.skip_condition and not stage.required:
                try:
                    if eval(stage.skip_condition, {"__builtins__": {}}, {"state": state}):  # noqa: S307
                        logger.debug("Skipping stage '%s' (condition met)", stage.name)
                        continue
                except Exception as exc:
                    logger.warning(
                        "Failed to evaluate skip_condition for stage '%s': %s",
                        stage.name, exc,
                    )
                    # On evaluation failure treat as not-skipped (safe default)

            # Check dependencies
            missing = [dep for dep in stage.depends_on if dep not in plan_set]
            if missing:
                logger.warning(
                    "Stage '%s' has unmet dependencies %s - skipping",
                    stage.name, missing,
                )
                continue

            plan.append(stage.name)
            plan_set.add(stage.name)

        return plan

    def validate_plan(self, plan: List[str]) -> bool:
        """Validate that *plan* respects all declared dependencies.

        Returns ``True`` when every stage's ``depends_on`` entries appear
        earlier in the plan list.
        """
        seen: set[str] = set()
        for name in plan:
            stage = self.get(name)
            if stage is None:
                logger.error("Plan references unknown stage '%s'", name)
                return False
            for dep in stage.depends_on:
                if dep not in seen:
                    logger.error(
                        "Stage '%s' depends on '%s' which has not run yet",
                        name, dep,
                    )
                    return False
            seen.add(name)
        return True


# ------------------------------------------------------------------
# Decorator helper
# ------------------------------------------------------------------

def register_stage(
    name: str,
    order: int,
    required: bool = True,
    depends_on: Tuple[str, ...] = (),
    skip_condition: Optional[str] = None,
):
    """Decorator that registers a function as a graph stage.

    Usage::

        @register_stage("router", order=10, required=True)
        def route_node(state):
            \"\"\"Route user request to appropriate agent(s).\"\"\"
            ...

    The decorated function is returned unchanged so it can still be
    referenced directly (e.g. in unit tests or the legacy ``build_graph``).
    """

    def decorator(fn: Callable) -> Callable:
        StageRegistry.get_instance().register(
            StageDefinition(
                name=name,
                node_fn=fn,
                description=fn.__doc__ or "",
                order=order,
                required=required,
                depends_on=depends_on,
                skip_condition=skip_condition,
            )
        )
        return fn

    return decorator


def get_stage_registry() -> StageRegistry:
    """Module-level convenience accessor for the singleton registry."""
    return StageRegistry.get_instance()
