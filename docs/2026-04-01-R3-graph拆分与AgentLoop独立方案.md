# R3: graph.py 拆分与 AgentLoop 独立方案

> 优先级：P1 | 预估工作量：2-3 天 | 依赖：R2（统一 MessageBus 后再拆分更干净） | 被依赖：R5（Plan Mode 在拆分后的 graph 上实现）

---

## 1. 目标

`core/graph.py` 当前 2200+ 行，包含：图定义、节点实现、辅助函数（乱码修复、Markdown 格式化、结构化提取）、条件路由、恢复逻辑，全部混在一个文件。新增节点或修改单个节点逻辑时需要理解整个文件。

本方案将 graph.py 拆分为职责清晰的多个模块，并引入 `LoopState` 追踪跨轮状态。

**验收标准**：

- `core/graph.py` 精简为纯图定义 + 边连接，目标 < 300 行
- 各节点函数可独立 import、独立测试
- 辅助函数（乱码修复等）移到 utils，不污染核心编排逻辑
- 引入 `LoopState` dataclass 追踪跨轮状态（replan_count、error_recovery 等）
- 全量测试 `pytest tests -q` 通过

---

## 2. 现有代码基础

| 组件 | 行号 | 行数 | 当前职责 |
|------|------|------|---------|
| MessageBus 辅助 | 48-74 | ~30 | `_get_bus` / `_save_bus` / `_bus_get_str` |
| 文本修复辅助 | 148-194 | ~50 | `_repair_mojibake_text` / `_normalize_text_value` / `_normalize_payload` |
| 结构化提取 | 209-280 | ~70 | `_extract_structured_findings` |
| 格式化输出 | ~416 | ~80 | `_build_deterministic_list_answer` + LLM 降噪 |
| 时间/位置 hint | 100-145 | ~45 | `_build_finalize_time_hint` / `_build_finalize_location_hint` |
| `_should_skip_for_resume` | 700-719 | ~20 | 断点恢复跳过逻辑 |
| `route_node` | ~770 | ~120 | 路由节点 |
| `plan_validator_node` | ~900 | ~80 | 规划验证节点 |
| `parallel_executor_node` | ~1000 | ~200 | 并行执行节点 |
| `critic_node` | ~1200 | ~120 | Critic 评审节点 |
| `validator_node` | ~1320 | ~100 | 验证器节点 |
| `human_confirm_node` | ~1420 | ~90 | 人工确认节点 |
| `replanner_node` | 1514-1592 | ~200 | 重规划节点 |
| `dynamic_replan_node` | ~1600 | ~100 | 动态重规划节点 |
| `finalize_node` | 1707-1780 | ~160 | 最终输出节点 |
| `_apply_adaptive_skip` | 2013-2029 | ~20 | 自适应跳过 |
| `build_graph_from_registry` | ~2036 | ~150 | 图构建 + 条件边 |

**关键发现**：

1. 节点函数内大量直接操作 `state` dict，无接口约束
2. `replanner_node` 内嵌 LLM 调用 + 失败分析 + 参数修复，应该是独立类
3. `finalize_node` 内嵌 LLM 调用 + 结构化提取 + 格式化，应该是独立类
4. 条件路由函数（`_route_after_*`）散布在各节点附近

---

## 3. 架构设计

### 3.1 拆分后的模块结构

```
core/
├── graph.py              ← 精简：图定义 + 边连接（<300 行）
├── graph_nodes.py        ← 所有节点函数（route, plan_validator, executor, critic, validator, human_confirm）
├── graph_conditions.py   ← 所有条件路由函数（_route_after_plan_validator, _route_after_critic 等）
├── replanner.py          ← Replanner 类（replanner_node + dynamic_replan_node 逻辑）
├── finalizer.py          ← Finalizer 类（finalize_node + 输出合成逻辑）
├── loop_state.py         ← LoopState dataclass
└── graph_utils.py        ← 辅助函数（_get_bus, _save_bus, _should_skip_for_resume, _apply_adaptive_skip）

utils/
├── text_repair.py        ← _repair_mojibake_text, _normalize_text_value 等
├── structured_extract.py ← _extract_structured_findings, _build_deterministic_list_answer
└── context_hints.py      ← _build_finalize_time_hint, _build_finalize_location_hint
```

### 3.2 拆分后的 graph.py 结构

```python
# core/graph.py — 目标 <300 行

from core.graph_nodes import (
    route_node, plan_validator_node, parallel_executor_node,
    critic_node, validator_node, human_confirm_node,
)
from core.graph_conditions import (
    route_after_plan_validator, route_after_critic,
    route_after_validator, route_after_human_confirm,
    route_after_executor,
)
from core.replanner import replanner_node, dynamic_replan_node
from core.finalizer import finalize_node


def build_graph():
    """构建 OmniCore 执行图"""
    graph = StateGraph(OmniCoreState)

    # 注册节点
    graph.add_node("route", route_node)
    graph.add_node("plan_validator", plan_validator_node)
    graph.add_node("parallel_executor", parallel_executor_node)
    # ... 其余节点 ...

    # 连接边
    graph.add_edge(START, "route")
    graph.add_conditional_edges("plan_validator", route_after_plan_validator, {...})
    # ... 其余边 ...

    return graph.compile(checkpointer=...)
```

### 3.3 LoopState 设计

```
LoopState:
┌─────────────────────────────────────────┐
│  replan_count: int = 0                  │  当前重规划次数
│  max_replan: int = 3                    │  最大重规划次数
│  error_recovery_count: int = 0          │  错误恢复次数
│  compact_applied: bool = False          │  是否已应用上下文压缩
│  current_stage: str = ""                │  当前执行阶段名
│  resume_after_stage: Optional[str]      │  断点恢复标记
│  adaptive_skip_applied: bool = False    │  是否已应用自适应跳过
│  turn_count: int = 0                    │  当前轮次（用于 R5 reminder）
└─────────────────────────────────────────┘
```

---

## 4. 详细设计

### 4.1 新建 `core/loop_state.py`

```python
"""跨轮执行状态追踪"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LoopState:
    """追踪 graph 执行过程中的跨轮状态"""

    replan_count: int = 0
    max_replan: int = 3
    error_recovery_count: int = 0
    compact_applied: bool = False
    current_stage: str = ""
    resume_after_stage: Optional[str] = None
    adaptive_skip_applied: bool = False
    turn_count: int = 0

    def can_replan(self) -> bool:
        return self.replan_count < self.max_replan

    def increment_replan(self) -> None:
        self.replan_count += 1

    def increment_turn(self) -> None:
        self.turn_count += 1

    def should_skip_stage(self, stage_order: int, resume_order: int) -> bool:
        """断点恢复时，判断是否跳过当前阶段"""
        if self.resume_after_stage is None:
            return False
        return stage_order <= resume_order

    def to_dict(self) -> dict:
        """序列化到 state（用于 checkpoint）"""
        return {
            "replan_count": self.replan_count,
            "error_recovery_count": self.error_recovery_count,
            "compact_applied": self.compact_applied,
            "current_stage": self.current_stage,
            "resume_after_stage": self.resume_after_stage,
            "adaptive_skip_applied": self.adaptive_skip_applied,
            "turn_count": self.turn_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LoopState":
        """从 state 反序列化"""
        if not data:
            return cls()
        return cls(
            replan_count=data.get("replan_count", 0),
            error_recovery_count=data.get("error_recovery_count", 0),
            compact_applied=data.get("compact_applied", False),
            current_stage=data.get("current_stage", ""),
            resume_after_stage=data.get("resume_after_stage"),
            adaptive_skip_applied=data.get("adaptive_skip_applied", False),
            turn_count=data.get("turn_count", 0),
        )
```

### 4.2 新建 `core/graph_nodes.py` — 节点函数

```python
"""OmniCore 图节点函数"""

from core.loop_state import LoopState
from core.graph_utils import get_bus, save_bus, should_skip_for_resume


def route_node(state: OmniCoreState) -> OmniCoreState:
    """路由节点：意图识别 + 任务分解"""
    loop = LoopState.from_dict(state.get("loop_state", {}))
    loop.current_stage = "route"
    loop.increment_turn()

    if should_skip_for_resume(loop, stage_order=10):
        state["loop_state"] = loop.to_dict()
        return state

    # --- 原 route_node 的核心逻辑，从 graph.py 搬过来 ---
    # router = RouterAgent(...)
    # result = router.analyze_intent(...)
    # state["task_queue"] = result["tasks"]
    # bus.publish("router", "*", MSG_ROUTER_RESULT, result)
    # ---

    state["loop_state"] = loop.to_dict()
    return state


def plan_validator_node(state: OmniCoreState) -> OmniCoreState:
    """规划验证节点"""
    # 从 graph.py 搬过来，逻辑不变
    ...


def parallel_executor_node(state: OmniCoreState) -> OmniCoreState:
    """并行执行节点"""
    # 从 graph.py 搬过来，逻辑不变
    ...


def critic_node(state: OmniCoreState) -> OmniCoreState:
    """Critic 评审节点"""
    ...


def validator_node(state: OmniCoreState) -> OmniCoreState:
    """验证器节点"""
    ...


def human_confirm_node(state: OmniCoreState) -> OmniCoreState:
    """人工确认节点"""
    ...
```

### 4.3 新建 `core/replanner.py`

```python
"""重规划逻辑：失败分析 + 重新规划"""

from core.loop_state import LoopState


class Replanner:
    """封装重规划的完整逻辑"""

    def __init__(self, llm_client, prompt_manager):
        self._llm = llm_client
        self._prompts = prompt_manager

    def analyze_failures(self, state: OmniCoreState) -> dict:
        """提取失败任务的 URL/错误类型/最后执行步骤"""
        # 从 graph.py replanner_node 搬过来的失败分析逻辑
        ...

    def generate_new_plan(self, failure_analysis: dict, state: OmniCoreState) -> list:
        """基于失败分析生成新的任务队列"""
        # 从 graph.py replanner_node 搬过来的 LLM 调用逻辑
        ...


def replanner_node(state: OmniCoreState) -> OmniCoreState:
    """重规划节点"""
    loop = LoopState.from_dict(state.get("loop_state", {}))

    if not loop.can_replan():
        state["execution_state"] = "max_replan_reached"
        return state

    replanner = Replanner(...)
    failure = replanner.analyze_failures(state)
    new_tasks = replanner.generate_new_plan(failure, state)

    state["task_queue"] = new_tasks
    loop.increment_replan()
    state["loop_state"] = loop.to_dict()
    return state


def dynamic_replan_node(state: OmniCoreState) -> OmniCoreState:
    """动态重规划节点（处理 dynamic_task_additions）"""
    # 从 graph.py 搬过来
    ...
```

### 4.4 新建 `core/finalizer.py`

```python
"""最终输出合成"""

from utils.structured_extract import extract_structured_findings
from utils.text_repair import normalize_payload
from utils.context_hints import build_finalize_time_hint, build_finalize_location_hint


class Finalizer:
    """封装最终输出合成的完整逻辑"""

    def __init__(self, llm_client, prompt_manager):
        self._llm = llm_client
        self._prompts = prompt_manager

    def build_delivery_summary(self, state: OmniCoreState) -> str:
        """构建结果摘要"""
        ...

    def synthesize_user_facing_answer(self, summary: str, state: OmniCoreState) -> str:
        """合成面向用户的最终回答"""
        ...


def finalize_node(state: OmniCoreState) -> OmniCoreState:
    """最终输出节点"""
    finalizer = Finalizer(...)
    # 从 graph.py 搬过来的逻辑
    ...
```

### 4.5 新建 `utils/text_repair.py`

```python
"""文本修复工具（乱码检测与修复）"""


def looks_like_mojibake(text: str) -> bool:
    """检测 latin-1→utf-8 乱码"""
    # 从 graph.py _looks_like_mojibake 搬过来
    ...


def repair_mojibake_text(text: str) -> str:
    """修复乱码文本"""
    # 从 graph.py _repair_mojibake_text 搬过来
    ...


def normalize_text_value(value) -> str:
    """递归对文本值应用乱码修复"""
    # 从 graph.py _normalize_text_value 搬过来
    ...


def normalize_payload(payload: dict) -> dict:
    """递归对 dict 中的所有文本值应用乱码修复"""
    # 从 graph.py _normalize_payload 搬过来
    ...
```

### 4.6 修改 `core/state.py` — 新增 loop_state 字段

```python
class OmniCoreState(TypedDict):
    # ... 现有字段 ...
    loop_state: Dict[str, Any]  # LoopState 的序列化形态
```

---

## 5. 典型场景

### 场景：拆分后新增一个"安全审查节点"

**拆分前**：需要在 2200 行的 graph.py 中找到正确位置，插入节点函数（~100 行），修改边连接。

**拆分后**：

```python
# 1. 在 core/graph_nodes.py 中新增函数
def security_review_node(state: OmniCoreState) -> OmniCoreState:
    """安全审查节点"""
    loop = LoopState.from_dict(state.get("loop_state", {}))
    loop.current_stage = "security_review"
    # ... 审查逻辑 ...
    state["loop_state"] = loop.to_dict()
    return state

# 2. 在 core/graph.py 中注册 + 连边（2 行）
graph.add_node("security_review", security_review_node)
graph.add_edge("parallel_executor", "security_review")
```

---

## 6. 实施步骤

| 步骤 | 任务 | 交付物 | 状态 |
|------|------|--------|------|
| 1 | 新建 `core/loop_state.py`（LoopState dataclass） | `core/loop_state.py` | 🔲 |
| 2 | 新建 `utils/text_repair.py`，从 graph.py 搬入乱码修复函数 | `utils/text_repair.py` | 🔲 |
| 3 | 新建 `utils/structured_extract.py`，搬入 `_extract_structured_findings` + `_build_deterministic_list_answer` | `utils/structured_extract.py` | 🔲 |
| 4 | 新建 `utils/context_hints.py`，搬入 `_build_finalize_time_hint` + `_build_finalize_location_hint` | `utils/context_hints.py` | 🔲 |
| 5 | 新建 `core/graph_utils.py`，搬入 `_get_bus` / `_save_bus` / `_should_skip_for_resume` / `_apply_adaptive_skip` | `core/graph_utils.py` | 🔲 |
| 6 | 新建 `core/graph_conditions.py`，搬入所有条件路由函数 | `core/graph_conditions.py` | 🔲 |
| 7 | 新建 `core/graph_nodes.py`，搬入 6 个节点函数（route/plan_validator/executor/critic/validator/human_confirm），集成 LoopState | `core/graph_nodes.py` | 🔲 |
| 8 | 新建 `core/replanner.py`（Replanner 类 + replanner_node + dynamic_replan_node） | `core/replanner.py` | 🔲 |
| 9 | 新建 `core/finalizer.py`（Finalizer 类 + finalize_node） | `core/finalizer.py` | 🔲 |
| 10 | 精简 `core/graph.py` 为纯图定义（import 各模块 + 注册节点 + 连边），目标 < 300 行 | `core/graph.py` | 🔲 |
| 11 | `core/state.py` 新增 `loop_state` 字段 | `core/state.py` | 🔲 |
| 12 | 全量回归测试 + 补充节点级单元测试 | `tests/` | 🔲 |

**注意**：步骤 2-6 可以并行做（互不依赖）。步骤 7-9 可以逐个搬迁，每搬一个跑一次测试。

---

## 7. 风险与注意事项

| 风险 | 缓解措施 |
|------|---------|
| 搬迁过程中漏掉节点间的隐式依赖（如闭包捕获的变量） | 每搬一个节点跑全量测试；搬迁前用 grep 确认所有引用点 |
| `LoopState` 序列化/反序列化可能与现有 checkpoint 不兼容 | `from_dict()` 对缺失字段提供默认值，兼容旧 checkpoint |
| 拆分后 import 循环（graph_nodes 依赖 graph_utils，graph_utils 依赖 state） | 严格单向依赖：graph.py → graph_nodes → graph_utils → state；utils/ 不依赖 core/ |
| 拆分改动面大，容易引入 regression | 分 PR 做：先搬 utils（低风险），再搬 nodes（中风险），最后精简 graph.py |
| StageRegistry（`build_graph_from_registry`）的装饰器注册机制需适配 | 装饰器保持在各节点函数上，只是文件位置变了 |
