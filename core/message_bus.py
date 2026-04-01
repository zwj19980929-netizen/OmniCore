"""
Typed Message Bus for OmniCore inter-agent communication.

Provides a structured, type-safe message passing protocol to replace
free-form shared_memory Dict access. Maintains full backward compatibility
with the legacy shared_memory format via bridge methods.
"""

import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Standard message type constants
# ---------------------------------------------------------------------------

MSG_DIRECT_ANSWER = "direct_answer"
MSG_TASK_RESULT = "task_result"
MSG_REPLAN_REQUEST = "replan_request"
MSG_REPLAN_HISTORY = "replan_history"
MSG_FINAL_INSTRUCTIONS = "final_instructions"
MSG_HIGH_RISK_REASON = "high_risk_reason"
MSG_APPROVED_ACTIONS = "approved_actions"
MSG_POLICY_DECISION = "policy_decision"
MSG_RESUME_STAGE = "resume_stage"
MSG_CONTEXT = "context"
MSG_USER_PREFERENCES = "user_preferences"
MSG_WORK_CONTEXT = "work_context"
MSG_MEMORY = "memory"

# Agent-to-Agent 协作消息类型 (P3-2)
MSG_AGENT_REQUEST = "agent_request"     # Agent A 请求 Agent B 执行子任务
MSG_AGENT_RESPONSE = "agent_response"   # Agent B 返回结果给 Agent A
MSG_AGENT_STATUS = "agent_status"       # Agent 状态广播（开始/完成/失败）


# ---------------------------------------------------------------------------
# Mapping from legacy shared_memory keys to (message_type, source, target)
# ---------------------------------------------------------------------------

_LEGACY_KEY_MAP: Dict[str, tuple] = {
    "router_direct_answer":       (MSG_DIRECT_ANSWER,       "router",  "finalize"),
    "router_high_risk_reason":    (MSG_HIGH_RISK_REASON,    "router",  "executor"),
    "_replan_history":            (MSG_REPLAN_HISTORY,       "system",  "*"),
    "_final_answer_instructions": (MSG_FINAL_INSTRUCTIONS,   "critic",  "finalize"),
    "_approved_actions":          (MSG_APPROVED_ACTIONS,      "policy",  "executor"),
    "_resume_after_stage":        (MSG_RESUME_STAGE,          "system",  "*"),
    "current_time_context":       (MSG_CONTEXT,              "system",  "*"),
    "current_location_context":   (MSG_CONTEXT,              "system",  "*"),
    "user_preferences":           (MSG_USER_PREFERENCES,      "system",  "*"),
    "work_context":               (MSG_WORK_CONTEXT,          "system",  "*"),
    "conversation_history":       (MSG_MEMORY,               "system",  "*"),
    "related_history":            (MSG_MEMORY,               "system",  "*"),
    "session_artifacts":          (MSG_MEMORY,               "system",  "*"),
    "resource_memory":            (MSG_MEMORY,               "system",  "*"),
    "successful_paths":           (MSG_MEMORY,               "system",  "*"),
    "failure_patterns":           (MSG_MEMORY,               "system",  "*"),
}


# ---------------------------------------------------------------------------
# Agent-to-Agent 协作消息 (P3-2)
# ---------------------------------------------------------------------------

@dataclass
class AgentRequest:
    """Agent A → Agent B 的任务请求。

    Attributes:
        from_agent: 发起请求的 Agent 标识。
        to_agent: 目标 Agent 标识。
        task: 需要执行的任务描述（与 TaskItem 兼容的 dict）。
        callback_task_id: 完成后更新哪个任务的结果（可选）。
    """
    from_agent: str
    to_agent: str
    task: Dict[str, Any]
    callback_task_id: str = ""

    def to_payload(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, data: Dict[str, Any]) -> "AgentRequest":
        known = {"from_agent", "to_agent", "task", "callback_task_id"}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class AgentResponse:
    """Agent B 的执行响应。

    Attributes:
        from_agent: 响应方 Agent 标识。
        to_agent: 请求方 Agent 标识。
        request_task_id: 对应的请求任务 ID。
        result: 执行结果。
        success: 是否成功。
    """
    from_agent: str
    to_agent: str
    request_task_id: str
    result: Dict[str, Any]
    success: bool

    def to_payload(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, data: Dict[str, Any]) -> "AgentResponse":
        known = {"from_agent", "to_agent", "request_task_id", "result", "success"}
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# AgentMessage dataclass
# ---------------------------------------------------------------------------

@dataclass
class AgentMessage:
    """A single typed message exchanged between agents on the bus.

    Attributes:
        source: Identifier of the sending agent (e.g. "router", "critic").
        target: Identifier of the intended recipient, or "*" for broadcast.
        message_type: One of the MSG_* constants, or a custom type string.
        payload: Arbitrary JSON-serializable data carried by the message.
        timestamp: Unix epoch timestamp of when the message was published.
        job_id: Optional job identifier for scoping messages to a run.
    """

    source: str
    target: str
    message_type: str
    payload: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    job_id: str = ""

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentMessage":
        """Deserialize from a dictionary.

        Unknown keys are silently ignored so that forward-compatible data
        produced by newer versions does not break older code.
        """
        known_fields = {"source", "target", "message_type", "payload",
                        "timestamp", "job_id"}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)


# ---------------------------------------------------------------------------
# MessageBus
# ---------------------------------------------------------------------------

class MessageBus:
    """Type-safe inter-agent message passing with namespace isolation.

    All public methods are thread-safe.  The bus is designed to be
    JSON-serializable so it can be persisted inside a LangGraph state
    checkpoint via ``to_dict`` / ``from_dict``.

    Typical usage inside a graph node::

        bus = MessageBus.from_dict(state.get("message_bus", []))
        bus.publish("router", "finalize", MSG_DIRECT_ANSWER,
                    {"answer": "Hello!"}, job_id=state["job_id"])
        state["message_bus"] = bus.to_dict()
    """

    def __init__(self) -> None:
        self._messages: List[AgentMessage] = []
        self._lock = threading.Lock()

    # -- publishing ----------------------------------------------------------

    def publish(
        self,
        source: str,
        target: str,
        message_type: str,
        payload: Dict[str, Any],
        job_id: str = "",
    ) -> AgentMessage:
        """Publish a message to the bus.

        Args:
            source: Identifier of the sending agent.
            target: Identifier of the intended recipient, or "*" for broadcast.
            message_type: A MSG_* constant or custom type string.
            payload: JSON-serializable data dict.
            job_id: Optional job identifier.

        Returns:
            The AgentMessage that was appended to the bus.
        """
        msg = AgentMessage(
            source=source,
            target=target,
            message_type=message_type,
            payload=payload,
            timestamp=time.time(),
            job_id=job_id,
        )
        with self._lock:
            self._messages.append(msg)
        return msg

    # -- querying ------------------------------------------------------------

    def query(
        self,
        *,
        target: Optional[str] = None,
        message_type: Optional[str] = None,
        source: Optional[str] = None,
        job_id: Optional[str] = None,
        latest_only: bool = False,
    ) -> List[AgentMessage]:
        """Query messages with filters.

        All filter parameters are optional.  When multiple are given they are
        combined with AND logic.  A ``target`` filter of ``"*"`` matches only
        broadcast messages; any other value matches both messages addressed
        specifically to that target **and** broadcast messages.

        Args:
            target: Filter by target agent (also matches broadcasts).
            message_type: Filter by message type constant.
            source: Filter by source agent.
            job_id: Filter by job identifier.
            latest_only: If True, return only the single most recent match.

        Returns:
            List of matching AgentMessage objects (newest last).
        """
        with self._lock:
            results: List[AgentMessage] = []
            for msg in self._messages:
                if message_type is not None and msg.message_type != message_type:
                    continue
                if source is not None and msg.source != source:
                    continue
                if job_id is not None and msg.job_id != job_id:
                    continue
                if target is not None:
                    if target == "*":
                        if msg.target != "*":
                            continue
                    else:
                        if msg.target != target and msg.target != "*":
                            continue
                results.append(msg)

            if latest_only and results:
                return [results[-1]]
            return results

    def get_latest(
        self,
        message_type: str,
        target: Optional[str] = None,
    ) -> Optional[AgentMessage]:
        """Get the most recent message of a given type.

        Args:
            message_type: The MSG_* constant to look for.
            target: Optional target filter. None matches any target.

        Returns:
            The most recent matching AgentMessage, or None.
        """
        hits = self.query(message_type=message_type, target=target,
                          latest_only=True)
        return hits[0] if hits else None

    def has(self, message_type: str, target: Optional[str] = None) -> bool:
        """Check if at least one message of the given type exists.

        Args:
            message_type: The MSG_* constant to look for.
            target: Optional target filter. None matches any target.

        Returns:
            True if a matching message is found.
        """
        return bool(self.query(message_type=message_type, target=target,
                               latest_only=True))

    @property
    def messages(self) -> List[AgentMessage]:
        """Return a shallow copy of all messages (thread-safe)."""
        with self._lock:
            return list(self._messages)

    def __len__(self) -> int:
        with self._lock:
            return len(self._messages)

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> List[Dict[str, Any]]:
        """Serialize the entire bus to a JSON-compatible list of dicts.

        Suitable for storage in LangGraph state checkpoints.
        """
        with self._lock:
            return [msg.to_dict() for msg in self._messages]

    @classmethod
    def from_dict(cls, data: List[Dict[str, Any]]) -> "MessageBus":
        """Deserialize from a list of dicts (as produced by ``to_dict``).

        Args:
            data: List of serialized AgentMessage dicts.

        Returns:
            A new MessageBus instance populated with the deserialized messages.
        """
        bus = cls()
        if not data:
            return bus
        for item in data:
            bus._messages.append(AgentMessage.from_dict(item))
        return bus

    # -- backward compatibility bridge ---------------------------------------

    def to_shared_memory(self) -> Dict[str, Any]:
        """Convert bus contents to legacy shared_memory format.

        This enables existing code that reads ``state["shared_memory"]`` to
        keep working unchanged.  The mapping is *lossy* -- only the latest
        value for each legacy key is represented, and messages that do not
        correspond to any legacy key are stored under their task_id (for
        task results) or skipped.

        Returns:
            A dict compatible with the legacy shared_memory schema.
        """
        result: Dict[str, Any] = {}

        # Build a reverse map: (message_type, source, target) -> legacy_key
        # For keys sharing the same message_type we need the payload key to
        # disambiguate, so we also store the original legacy key for matching.
        reverse_map: Dict[str, List[str]] = {}
        for legacy_key, (mtype, _src, _tgt) in _LEGACY_KEY_MAP.items():
            reverse_map.setdefault(mtype, []).append(legacy_key)

        with self._lock:
            for msg in self._messages:
                mtype = msg.message_type

                # Task results are stored under their task_id
                if mtype == MSG_TASK_RESULT:
                    task_id = msg.payload.get("task_id", msg.source)
                    result[task_id] = msg.payload.get("data", msg.payload)
                    continue

                # Try to map back to a legacy key
                if mtype in reverse_map:
                    candidates = reverse_map[mtype]
                    if len(candidates) == 1:
                        result[candidates[0]] = msg.payload.get("value", msg.payload)
                    else:
                        # Multiple legacy keys share this message_type.
                        # Use the "legacy_key" hint in the payload if present,
                        # otherwise use the first candidate.
                        legacy_key = msg.payload.get("_legacy_key")
                        if legacy_key and legacy_key in candidates:
                            result[legacy_key] = msg.payload.get("value", msg.payload)
                        else:
                            # Fall back: store under all matching keys if
                            # the payload carries a value for them
                            for ck in candidates:
                                if ck in msg.payload:
                                    result[ck] = msg.payload[ck]
                    continue

                # Unknown message types: store under source as a fallback
                # so that task executors can find them
                if "task_id" in msg.payload:
                    result[msg.payload["task_id"]] = msg.payload.get("data", msg.payload)

        return result

    @classmethod
    def from_shared_memory(cls, shared_memory: Dict[str, Any]) -> "MessageBus":
        """Import legacy shared_memory contents into a new MessageBus.

        Each recognized key is mapped to a properly typed AgentMessage.
        Unrecognized keys are imported as MSG_TASK_RESULT messages with
        source set to the key name.

        Args:
            shared_memory: The legacy shared_memory dict from OmniCoreState.

        Returns:
            A new MessageBus populated from the shared_memory contents.
        """
        bus = cls()
        if not shared_memory or not isinstance(shared_memory, dict):
            return bus

        now = time.time()
        handled_keys: set = set()

        for legacy_key, (mtype, source, target) in _LEGACY_KEY_MAP.items():
            if legacy_key not in shared_memory:
                continue
            handled_keys.add(legacy_key)
            value = shared_memory[legacy_key]
            payload: Dict[str, Any] = {
                "value": value,
                "_legacy_key": legacy_key,
            }
            bus._messages.append(AgentMessage(
                source=source,
                target=target,
                message_type=mtype,
                payload=payload,
                timestamp=now,
            ))

        # Import remaining keys as task results
        for key, value in shared_memory.items():
            if key in handled_keys:
                continue
            bus._messages.append(AgentMessage(
                source=key,
                target="*",
                message_type=MSG_TASK_RESULT,
                payload={"task_id": key, "data": value},
                timestamp=now,
            ))

        return bus

    # -- utility -------------------------------------------------------------

    def clear(self) -> None:
        """Remove all messages from the bus."""
        with self._lock:
            self._messages.clear()

    def __repr__(self) -> str:
        with self._lock:
            count = len(self._messages)
        return f"<MessageBus messages={count}>"
