"""
OmniCore 核心状态定义
基于 LangGraph 的 TypedDict 状态管理
"""
from typing import TypedDict, Annotated, List, Dict, Any, Optional
from langgraph.graph.message import add_messages


class TaskItem(TypedDict):
    """单个子任务结构"""
    task_id: str
    task_type: str          # web_scraping, file_operation, system_control
    description: str
    params: Dict[str, Any]
    status: str             # pending, running, completed, failed
    result: Optional[Any]
    priority: int           # 优先级 1-10，数字越大优先级越高


class OmniCoreState(TypedDict):
    """
    OmniCore 核心状态 - LangGraph 图的底层 State
    所有 Agent 共享此状态进行协作
    """
    # 记录所有 Agent 之间的对话和系统提示
    messages: Annotated[list, add_messages]

    # 用户原始输入
    user_input: str

    # 路由器的当前意图解析
    current_intent: str

    # 意图解析的置信度 (0-1)
    intent_confidence: float

    # 子任务队列 (DAG 的执行顺序)
    task_queue: List[TaskItem]

    # 当前正在执行的任务索引
    current_task_index: int

    # 各个 Worker 抓取或处理后的中间数据暂存区
    shared_memory: Dict[str, Any]

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

    # 执行状态: idle, routing, executing, reviewing, completed, error
    execution_status: str


def create_initial_state(user_input: str) -> OmniCoreState:
    """创建初始状态"""
    return OmniCoreState(
        messages=[],
        user_input=user_input,
        current_intent="",
        intent_confidence=0.0,
        task_queue=[],
        current_task_index=0,
        shared_memory={},
        critic_feedback="",
        critic_approved=False,
        human_approved=False,
        needs_human_confirm=False,
        error_trace="",
        final_output="",
        execution_status="idle",
    )
