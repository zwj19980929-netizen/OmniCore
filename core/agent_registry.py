"""
Agent Registry - data-driven agent type management.

Replaces hardcoded TaskType enum with a dynamic registry loaded from config.
New agent types can be added by editing config/agents.yaml without code changes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Dict, FrozenSet, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "agents.yaml"


@dataclass(frozen=True)
class AgentDefinition:
    """Immutable definition of an agent type loaded from configuration."""

    name: str
    adapter: str
    description: str
    description_zh: str
    capabilities: Tuple[str, ...]
    max_parallelism: int
    risk_level: str  # "low", "medium", "high"


class AgentRegistry:
    """Registry for available agent types, loaded from YAML config.

    Provides singleton access via ``get_instance()`` so that every part of the
    system shares the same view of registered agents.  The registry is
    populated from ``config/agents.yaml`` on first access but also supports
    dynamic registration for plugin-provided agent types.
    """

    _instance: ClassVar[Optional[AgentRegistry]] = None

    def __init__(self, config_path: Optional[str] = None):
        self._agents: Dict[str, AgentDefinition] = {}
        self._load(config_path)

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> AgentRegistry:
        """Return the singleton registry, creating it on first call."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton (useful for testing)."""
        cls._instance = None

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load(self, config_path: Optional[str] = None) -> None:
        """Load agent definitions from the YAML config file."""
        path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
        if not path.exists():
            logger.warning("Agent config file not found: %s", path)
            return

        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        except Exception as exc:
            logger.error("Failed to load agent config from %s: %s", path, exc)
            return

        agents_section = raw.get("agents", {})
        if not isinstance(agents_section, dict):
            logger.error("Invalid 'agents' section in %s (expected mapping)", path)
            return

        for name, cfg in agents_section.items():
            if not isinstance(cfg, dict):
                logger.warning("Skipping malformed agent entry: %s", name)
                continue
            try:
                agent_def = AgentDefinition(
                    name=str(name),
                    adapter=str(cfg.get("adapter", name)),
                    description=str(cfg.get("description", "")),
                    description_zh=str(cfg.get("description_zh", "")),
                    capabilities=tuple(cfg.get("capabilities", ())),
                    max_parallelism=int(cfg.get("max_parallelism", 1)),
                    risk_level=str(cfg.get("risk_level", "medium")),
                )
                self._agents[agent_def.name] = agent_def
            except (TypeError, ValueError) as exc:
                logger.warning("Skipping agent '%s' due to invalid config: %s", name, exc)

        logger.info("AgentRegistry loaded %d agent type(s) from %s", len(self._agents), path)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, name: str) -> Optional[AgentDefinition]:
        """Get an agent definition by name, or ``None`` if not found."""
        return self._agents.get(name)

    def list_agents(self) -> List[AgentDefinition]:
        """Return all registered agent definitions (insertion order)."""
        return list(self._agents.values())

    def get_by_capability(self, capability: str) -> List[AgentDefinition]:
        """Return agents that advertise *capability*."""
        return [a for a in self._agents.values() if capability in a.capabilities]

    def is_valid_type(self, agent_type: str) -> bool:
        """Check whether *agent_type* is a registered name."""
        return agent_type in self._agents

    def get_supported_types(self) -> FrozenSet[str]:
        """Return a frozen set of all registered type names.

        This is the dynamic replacement for the ``SUPPORTED_TASK_TYPES``
        constant in ``core.constants``.
        """
        return frozenset(self._agents.keys())

    # ------------------------------------------------------------------
    # Router prompt generation
    # ------------------------------------------------------------------

    def build_router_agent_descriptions(self, lang: str = "zh") -> str:
        """Generate a human-readable description block for Router prompts.

        Parameters
        ----------
        lang:
            ``"zh"`` for Chinese descriptions, ``"en"`` for English.

        Returns
        -------
        str
            A formatted multi-line string listing every agent type, its
            description, capabilities, and risk level.
        """
        lines: List[str] = []
        for agent in self._agents.values():
            desc = agent.description_zh if lang == "zh" else agent.description
            caps = ", ".join(agent.capabilities) if agent.capabilities else "general"
            lines.append(
                f"- **{agent.name}**: {desc}  "
                f"(capabilities: {caps} | parallelism: {agent.max_parallelism} | risk: {agent.risk_level})"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Dynamic registration (plugins)
    # ------------------------------------------------------------------

    def register(self, agent_def: AgentDefinition) -> None:
        """Dynamically register a new agent type at runtime.

        This is intended for plugin-provided agent types that are not part of
        the static ``agents.yaml`` configuration.  Overwrites any existing
        definition with the same name and logs a warning in that case.
        """
        if agent_def.name in self._agents:
            logger.warning(
                "Overwriting existing agent definition '%s' via dynamic registration",
                agent_def.name,
            )
        self._agents[agent_def.name] = agent_def
        logger.info("Dynamically registered agent type '%s'", agent_def.name)

    def unregister(self, name: str) -> bool:
        """Remove a dynamically registered agent type.

        Returns ``True`` if the agent was found and removed.
        """
        removed = self._agents.pop(name, None)
        if removed is not None:
            logger.info("Unregistered agent type '%s'", name)
        return removed is not None


def get_agent_registry() -> AgentRegistry:
    """Module-level convenience accessor for the singleton registry."""
    return AgentRegistry.get_instance()
