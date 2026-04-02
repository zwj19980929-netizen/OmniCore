"""
OmniCore 核心状态定义
基于 LangGraph 的 TypedDict 状态管理
"""
from typing import TypedDict, Annotated, List, Dict, Any, Optional
from langgraph.graph.message import add_messages


class _TaskItemRequired(TypedDict):
    """TaskItem 必填字段（v0.1 兼容）"""
    task_id: str
    task_type: str          # web_worker, file_worker, system_worker, browser_agent
    description: str
    params: Dict[str, Any]
    status: str             # pending, running, completed, failed
    result: Optional[Any]
    priority: int           # 优先级 1-10，数字越大优先级越高


class TaskItem(_TaskItemRequired, total=False):
    """单个子任务结构（v0.2 扩展）"""
    success_criteria: List[str]           # ["len(result.data) >= 5"]
    fallbacks: List[Dict[str, Any]]       # [{"type":"retry","param_patch":{}}]
    abort_conditions: List[str]           # ["manual_cancelled"]
    depends_on: List[str]                 # 依赖的 task_id 列表
    failure_type: Optional[str]           # timeout/selector_not_found/blocked_or_captcha/permission_denied/invalid_input/unknown
    execution_trace: List[Dict[str, Any]] # [{step_no, plan, action, observation, decision}]
    required_capabilities: List[str]      # 所需模型能力 ["text_chat", "vision", "image_gen", ...]
    tool_name: str
    risk_level: str
    estimated_cost: str                   # "low"/"medium"/"high" — 预估执行成本
    requires_confirmation: bool
    policy_reason: str
    affected_resources: List[str]


class ArtifactItem(TypedDict, total=False):
    artifact_id: str
    session_id: str
    job_id: str
    task_id: str
    created_at: str
    artifact_type: str
    source_key: str
    path: str
    name: str
    task_type: str
    tool_name: str


class PolicyDecisionItem(TypedDict, total=False):
    task_id: str
    tool_name: str
    action: str
    target_resource: str
    risk_level: str
    decision: str
    reason: str
    requires_human_confirm: bool
    approved_by: str
    approved_at: str


def ensure_task_defaults(task: TaskItem) -> TaskItem:
    """为缺失的 v0.2 可选字段填充默认值（就地修改并返回）"""
    task.setdefault("success_criteria", [])
    task.setdefault("fallbacks", [])
    task.setdefault("abort_conditions", [])
    task.setdefault("depends_on", [])
    task.setdefault("failure_type", None)
    task.setdefault("execution_trace", [])
    task.setdefault("required_capabilities", [])
    task.setdefault("tool_name", "")
    task.setdefault("risk_level", "medium")
    task.setdefault("estimated_cost", "medium")
    task.setdefault("requires_confirmation", False)
    task.setdefault("policy_reason", "")
    task.setdefault("affected_resources", [])
    return task


class OmniCoreState(TypedDict):
    """
    OmniCore 核心状态 - LangGraph 图的底层 State
    所有 Agent 共享此状态进行协作
    """
    # 记录所有 Agent 之间的对话和系统提示
    messages: Annotated[list, add_messages]

    # 用户原始输入
    user_input: str
    session_id: str
    job_id: str

    # 路由器的当前意图解析
    current_intent: str

    # 意图解析的置信度 (0-1)
    intent_confidence: float

    # 子任务队列 (DAG 的执行顺序)
    task_queue: List[TaskItem]

    # 当前正在执行的任务索引
    current_task_index: int

    # Typed message bus — sole inter-node communication channel (R2)
    # Serialized form, see core.message_bus.MessageBus
    message_bus: List[Dict[str, Any]]

    artifacts: List[ArtifactItem]
    policy_decisions: List[PolicyDecisionItem]

    # 独立审查官 (Critic) 的反馈
    critic_feedback: str

    # Critic 审核是否通过
    critic_approved: bool

    # 强制安全锁：执行高危操作前必须为 True
    human_approved: bool

    # 是否需要人类确认
    needs_human_confirm: bool

    # 错误日志追踪
    error_trace: str

    # 最终输出结果
    final_output: str
    delivery_package: Dict[str, Any]

    # 执行状态: idle, routing, executing, reviewing, completed, error
    execution_status: str

    # 重规划计数（防止无限循环）
    replan_count: int

    # Validator 硬规则校验是否通过
    validator_passed: bool

    # Skill Library: 匹配到的 Skill ID（用于 finalize 阶段反馈更新）
    matched_skill_id: str

    # ── 多 Agent 协作 (P3-2) ──────────────────────────────
    # 类型化的任务间数据传递：task_id → 结构化输出
    task_outputs: Dict[str, Any]
    # 动态规划：Planner 可在执行中插入新任务
    dynamic_task_additions: List[Dict[str, Any]]
    # Agent 间点对点消息队列
    agent_messages: List[Dict[str, Any]]


def create_initial_state(
    user_input: str,
    session_id: str = "",
    job_id: str = "",
) -> OmniCoreState:
    """创建初始状态"""
    return OmniCoreState(
        messages=[],
        user_input=user_input,
        session_id=session_id,
        job_id=job_id,
        current_intent="",
        intent_confidence=0.0,
        task_queue=[],
        current_task_index=0,
        message_bus=[],
        artifacts=[],
        policy_decisions=[],
        critic_feedback="",
        critic_approved=False,
        human_approved=False,
        needs_human_confirm=False,
        error_trace="",
        final_output="",
        delivery_package={},
        execution_status="idle",
        replan_count=0,
        validator_passed=True,
        matched_skill_id="",
        task_outputs={},
        dynamic_task_additions=[],
        agent_messages=[],
    )
