"""
Typed Message Bus for OmniCore inter-agent communication.

Provides a structured, type-safe message passing protocol for all
inter-node communication. This is the sole channel for state exchange
between graph nodes — shared_memory has been removed (R2).
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

# Agent-to-Agent collaboration message types (P3-2)
MSG_AGENT_REQUEST = "agent_request"
MSG_AGENT_RESPONSE = "agent_response"
MSG_AGENT_STATUS = "agent_status"

# Subagent / Coordinator message types (S5)
MSG_SUBAGENT_STARTED = "subagent_started"
MSG_SUBAGENT_COMPLETED = "subagent_completed"
MSG_SUBAGENT_FAILED = "subagent_failed"
MSG_SUBAGENT_APPROVAL_REQUEST = "subagent_approval_request"
MSG_SUBAGENT_APPROVAL_RESPONSE = "subagent_approval_response"
MSG_COORDINATOR_DISPATCH = "coordinator_dispatch"
MSG_COORDINATOR_SYNTHESIS = "coordinator_synthesis"

# R2: Context data message types (replacing shared_memory keys)
MSG_CONVERSATION_HISTORY = "conversation_history"
MSG_RELATED_HISTORY = "related_history"
MSG_SESSION_ARTIFACTS = "session_artifacts"
MSG_TIME_CONTEXT = "time_context"
MSG_LOCATION_CONTEXT = "location_context"
MSG_OS_CONTEXT = "os_context"
MSG_RESOURCE_MEMORY = "resource_memory"
MSG_SUCCESSFUL_PATHS = "successful_paths"
MSG_FAILURE_PATTERNS = "failure_patterns"
MSG_WORK_SCOPE = "work_scope"
MSG_MEMORY_SCOPE = "memory_scope"
MSG_RESUME_CHECKPOINT_ID = "resume_checkpoint_id"
MSG_RESUME_REQUESTED_AT = "resume_requested_at"
MSG_APPROVAL_RESUMED_AT = "approval_resumed_at"


# ---------------------------------------------------------------------------
# Agent-to-Agent collaboration messages (P3-2)
# ---------------------------------------------------------------------------

@dataclass
class AgentRequest:
    """Agent A -> Agent B task request."""
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
    """Agent B execution response."""
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
        """Deserialize from a dictionary."""
        known_fields = {"source", "target", "message_type", "payload",
                        "timestamp", "job_id"}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)


# ---------------------------------------------------------------------------
# MessageBus
# ---------------------------------------------------------------------------

class MessageBus:
    """Type-safe inter-agent message passing with namespace isolation.

    All public methods are thread-safe. The bus is designed to be
    JSON-serializable so it can be persisted inside a LangGraph state
    checkpoint via ``to_dict`` / ``from_dict``.

    Typical usage inside a graph node::

        bus = MessageBus.from_dict(state.get("message_bus", []))
        bus.publish("router", "finalize", MSG_DIRECT_ANSWER,
                    {"answer": "Hello!"}, job_id=state["job_id"])
        state["message_bus"] = bus.to_dict()
    """

    def __init__(self, *, ttl: int = 0, max_capacity: int = 500) -> None:
        self._messages: List[AgentMessage] = []
        self._lock = threading.Lock()
        self._ttl = ttl
        self._max_capacity = max_capacity

    # -- publishing ----------------------------------------------------------

    def publish(
        self,
        source: str,
        target: str,
        message_type: str,
        payload: Dict[str, Any],
        job_id: str = "",
    ) -> AgentMessage:
        """Publish a message to the bus."""
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
            # Capacity limit: trim oldest messages
            if self._max_capacity > 0 and len(self._messages) > self._max_capacity:
                self._messages = self._messages[-self._max_capacity:]
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

        All filter parameters are optional. When multiple are given they are
        combined with AND logic. A ``target`` filter of ``"*"`` matches only
        broadcast messages; any other value matches both messages addressed
        specifically to that target **and** broadcast messages.
        """
        now = time.time()
        with self._lock:
            results: List[AgentMessage] = []
            for msg in self._messages:
                # TTL filter
                if self._ttl > 0 and (now - msg.timestamp) > self._ttl:
                    continue
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
        """Get the most recent message of a given type."""
        hits = self.query(message_type=message_type, target=target,
                          latest_only=True)
        return hits[0] if hits else None

    def has(self, message_type: str, target: Optional[str] = None) -> bool:
        """Check if at least one message of the given type exists."""
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

    # -- snapshot for workers ------------------------------------------------

    def to_snapshot(self) -> Dict[str, Any]:
        """Build a flat dict snapshot for worker execution.

        Workers (web_worker, file_worker, etc.) receive a flat dict of
        context values for their execution. This replaces the old
        ``state["shared_memory"]`` snapshot passed to workers.
        """
        snapshot: Dict[str, Any] = {}
        with self._lock:
            now = time.time()
            for msg in self._messages:
                if self._ttl > 0 and (now - msg.timestamp) > self._ttl:
                    continue
                mtype = msg.message_type
                value = msg.payload.get("value", msg.payload)

                # Map message types to legacy-compatible flat keys
                # that workers expect to read
                if mtype == MSG_CONVERSATION_HISTORY:
                    snapshot["conversation_history"] = value
                elif mtype == MSG_USER_PREFERENCES:
                    snapshot["user_preferences"] = value
                elif mtype == MSG_TIME_CONTEXT:
                    snapshot["current_time_context"] = value
                elif mtype == MSG_LOCATION_CONTEXT:
                    snapshot["current_location_context"] = value
                elif mtype == MSG_OS_CONTEXT:
                    snapshot["current_os_context"] = value
                elif mtype == MSG_WORK_CONTEXT:
                    snapshot["work_context"] = value
                elif mtype == MSG_RESOURCE_MEMORY:
                    snapshot["resource_memory"] = value
                elif mtype == MSG_SUCCESSFUL_PATHS:
                    snapshot["successful_paths"] = value
                elif mtype == MSG_FAILURE_PATTERNS:
                    snapshot["failure_patterns"] = value
                elif mtype == MSG_REPLAN_HISTORY:
                    snapshot["_replan_history"] = value
                elif mtype == MSG_APPROVED_ACTIONS:
                    snapshot["_approved_actions"] = value
                elif mtype == MSG_RESUME_STAGE:
                    snapshot["_resume_after_stage"] = value
                elif mtype == MSG_RELATED_HISTORY:
                    snapshot["related_history"] = value
                elif mtype == MSG_SESSION_ARTIFACTS:
                    snapshot["session_artifacts"] = value
                elif mtype == MSG_TASK_RESULT:
                    task_id = msg.payload.get("task_id", msg.source)
                    snapshot[task_id] = msg.payload.get("data", msg.payload)
        return snapshot

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> List[Dict[str, Any]]:
        """Serialize the entire bus to a JSON-compatible list of dicts."""
        with self._lock:
            return [msg.to_dict() for msg in self._messages]

    @classmethod
    def from_dict(cls, data: List[Dict[str, Any]]) -> "MessageBus":
        """Deserialize from a list of dicts (as produced by ``to_dict``)."""
        from config.settings import settings as _settings
        bus = cls(
            ttl=_settings.MESSAGE_BUS_TTL,
            max_capacity=_settings.MESSAGE_BUS_MAX_CAPACITY,
        )
        if not data:
            return bus
        for item in data:
            bus._messages.append(AgentMessage.from_dict(item))
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
