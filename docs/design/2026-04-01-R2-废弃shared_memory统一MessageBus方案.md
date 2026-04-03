# R2: 废弃 shared_memory，统一 MessageBus 方案

> 优先级：P0 | 预估工作量：1-2 天 | 依赖：无 | 被依赖：R3（graph 拆分需先统一通信机制）

---

## 1. 目标

当前 `shared_memory`（plain dict）和 `MessageBus` 并存，graph 节点有的写 shared_memory，有的写 message_bus，有的两边都写。`MessageBus.to_shared_memory()` 桥接逻辑复杂、有损、依赖 `_legacy_key` hint 消歧。这种双写导致：

- 新增节点时不知道该写哪边
- 数据在两个容器中不一致
- 桥接代码本身成为 bug 温床

本方案**彻底移除 `shared_memory`**，所有节点间通信统一走 `MessageBus`。

**验收标准**：

- `shared_memory` 在源码中零出现（除历史文档和注释）
- `MessageBus` 的 `to_shared_memory()` / `from_shared_memory()` / `_LEGACY_KEY_MAP` 全部移除
- `MessageBus` 新增 TTL（默认 30 分钟）和最大容量（默认 500 条），防止内存失控
- 全量测试 `pytest tests -q` 通过

---

## 2. 现有代码基础

| 组件 | 文件 | 当前状态 |
|------|------|---------|
| State 定义 | `core/state.py` | `shared_memory: Dict[str, Any]` 与 `message_bus: List[Dict]` 并存 |
| MessageBus | `core/message_bus.py` | 完整 API（publish/query/get_latest/has），含 ~14 个 legacy key 桥接 |
| Graph 辅助 | `core/graph.py` 行 48-74 | `_get_bus()` / `_save_bus()` / `_bus_get_str()` 双写辅助层 |
| Task Executor | `core/task_executor.py` | `_apply_task_outcome()` 写 `state["shared_memory"][task_id]` |
| Router | `core/router.py` | 读取部分 shared_memory 值作为上下文 |
| Agents | `agents/*.py` | 部分 agent 通过 shared_memory 传递中间结果 |

**关键发现**：

1. `graph.py` 行 48-74 的 `_get_bus()` / `_save_bus()` 是双写枢纽——每次 `_save_bus()` 都会同时调用 `bus.to_shared_memory()` 写回 shared_memory
2. `_LEGACY_KEY_MAP` 硬编码了约 14 个键的映射，包括 `router_result`、`plan_result`、`critic_feedback` 等
3. `_apply_task_outcome()` 在 `task_outputs` 之外还写 `shared_memory[task_id]`，数据重复
4. `resume_job_from_checkpoint()` 用 `shared_memory["_resume_after_stage"]` 传递恢复位置

---

## 3. 架构设计

### 3.1 迁移前后对比

```
迁移前：
┌──────────┐     write      ┌───────────────┐
│  节点 A  │ ──────────────→│ shared_memory │
└──────────┘                └───────┬───────┘
                                    │ to_shared_memory()
┌──────────┐     publish    ┌───────▼───────┐
│  节点 B  │ ──────────────→│  MessageBus   │
└──────────┘                └───────────────┘
    两条路径，数据不一致风险

迁移后：
┌──────────┐     publish    ┌───────────────┐
│  节点 A  │ ──────────────→│  MessageBus   │
└──────────┘                │               │
┌──────────┐     publish    │  TTL + 容量   │
│  节点 B  │ ──────────────→│  自动清理     │
└──────────┘                └───────────────┘
    统一路径，类型安全
```

### 3.2 shared_memory 键 → MessageBus 消息映射

基于 `_LEGACY_KEY_MAP` 分析，需要迁移的键：

| shared_memory 键 | 迁移后的 MessageBus 消息 | message_type | source | target |
|---|---|---|---|---|
| `router_result` | 路由结果 | `MSG_ROUTER_RESULT` | `router` | `*` |
| `plan_result` | 规划结果 | `MSG_PLAN_RESULT` | `planner` | `*` |
| `critic_feedback` | Critic 反馈 | `MSG_CRITIC_FEEDBACK` | `critic` | `*` |
| `validator_result` | 验证结果 | `MSG_VALIDATOR_RESULT` | `validator` | `*` |
| `final_answer` | 最终答案 | `MSG_FINAL_ANSWER` | `finalize` | `*` |
| `task_{id}` | 任务输出 | `MSG_TASK_OUTPUT` | `executor` | `*` |
| `_resume_after_stage` | 恢复标记 | `MSG_RESUME_MARKER` | `runtime` | `*` |
| 其余 ~7 个 | 逐个分析 | 按语义命名 | 按来源 | 按目标 |

### 3.3 MessageBus TTL + 容量增强

```
MessageBus 内部：
┌─────────────────────────────────────────┐
│  messages: List[AgentMessage]           │
│                                         │
│  publish() 时：                         │
│    1. 追加新消息                        │
│    2. 如果 len > max_capacity:          │
│       删除最旧的非活跃消息              │
│    3. 如果消息.timestamp + ttl < now:    │
│       标记为过期（lazy cleanup）        │
│                                         │
│  query() 时：                           │
│    过滤掉 expired 消息                  │
└─────────────────────────────────────────┘
```

---

## 4. 详细设计

### 4.1 新增 MessageBus 消息类型常量

在 `core/constants.py` 中追加：

```python
# ── MessageBus 标准消息类型 ──
MSG_ROUTER_RESULT = "router_result"
MSG_PLAN_RESULT = "plan_result"
MSG_CRITIC_FEEDBACK = "critic_feedback"
MSG_VALIDATOR_RESULT = "validator_result"
MSG_FINAL_ANSWER = "final_answer"
MSG_TASK_OUTPUT = "task_output"
MSG_RESUME_MARKER = "resume_marker"
MSG_EXECUTION_STATE = "execution_state"
```

### 4.2 修改 `core/message_bus.py` — 增加 TTL 和容量限制

```python
import time
from config.settings import MESSAGE_BUS_TTL, MESSAGE_BUS_MAX_CAPACITY


class MessageBus:
    def __init__(self):
        self._messages: List[AgentMessage] = []
        self._lock = threading.Lock()
        self._ttl = MESSAGE_BUS_TTL          # 默认 1800 秒（30 分钟）
        self._max_capacity = MESSAGE_BUS_MAX_CAPACITY  # 默认 500

    def publish(self, source, target, message_type, payload, job_id=""):
        with self._lock:
            msg = AgentMessage(
                source=source, target=target,
                message_type=message_type, payload=payload,
                timestamp=time.time(), job_id=job_id,
            )
            self._messages.append(msg)

            # 容量限制：超出时删除最旧消息
            if len(self._messages) > self._max_capacity:
                self._messages = self._messages[-self._max_capacity:]

            return msg

    def query(self, *, target=None, message_type=None, source=None,
              job_id=None, latest_only=False):
        now = time.time()
        with self._lock:
            results = []
            for m in self._messages:
                # TTL 过滤
                if self._ttl > 0 and (now - m.timestamp) > self._ttl:
                    continue
                # 条件过滤（原有逻辑不变）
                if target and m.target not in (target, "*"):
                    continue
                if message_type and m.message_type != message_type:
                    continue
                if source and m.source != source:
                    continue
                if job_id and m.job_id != job_id:
                    continue
                results.append(m)

            if latest_only and results:
                return [results[-1]]
            return results

    # 删除以下方法：
    # def to_shared_memory(self) -> Dict[str, Any]: ...
    # def from_shared_memory(cls, shared_memory) -> MessageBus: ...
    # _LEGACY_KEY_MAP = { ... }
```

### 4.3 修改 `core/state.py` — 移除 shared_memory

```python
class OmniCoreState(TypedDict):
    messages: Annotated[list, add_messages]
    task_queue: List[TaskItem]
    # shared_memory: Dict[str, Any]   ← 删除此行
    message_bus: List[Dict[str, Any]]
    task_outputs: Dict[str, Any]
    # ... 其余字段不变 ...
```

### 4.4 修改 `core/graph.py` — 移除双写辅助层

```python
# 删除 _get_bus / _save_bus / _bus_get_str 中的 shared_memory 操作

def _get_bus(state: OmniCoreState) -> MessageBus:
    """从 state 反序列化 MessageBus"""
    raw = state.get("message_bus", [])
    if isinstance(raw, MessageBus):
        return raw
    bus = MessageBus()
    if raw:
        bus = MessageBus.from_dict(raw)
    return bus


def _save_bus(state: OmniCoreState, bus: MessageBus) -> None:
    """序列化 MessageBus 回 state"""
    state["message_bus"] = bus.to_dict()
    # 不再调用 bus.to_shared_memory()
    # 不再写 state["shared_memory"]
```

### 4.5 修改 `core/task_executor.py` — 移除 shared_memory 写入

```python
def _apply_task_outcome(state, idx, outcome):
    task = state["task_queue"][idx]
    task["status"] = outcome.get("status", task["status"])
    task["result"] = outcome.get("result")
    # ... 其余字段 ...

    # 删除以下行：
    # if outcome.get("shared_memory"):
    #     state["shared_memory"][task_id] = outcome["shared_memory"]

    # task_outputs 保留（这是类型化的跨任务引用，不是 shared_memory）
    if task["status"] == "completed":
        typed_output = _extract_typed_output(task, outcome)
        if typed_output:
            state["task_outputs"][task_id] = typed_output
```

### 4.6 修改 `core/runtime.py` — resume 标记迁移

```python
def resume_job_from_checkpoint(self, ...):
    # 原来：
    # resumed_state["shared_memory"]["_resume_after_stage"] = stage_name

    # 改为：
    bus = MessageBus.from_dict(resumed_state.get("message_bus", []))
    bus.publish(
        source="runtime",
        target="*",
        message_type=MSG_RESUME_MARKER,
        payload={"resume_after_stage": stage_name},
    )
    resumed_state["message_bus"] = bus.to_dict()
```

### 4.7 修改 `config/settings.py` — 新增配置项

```python
# ── MessageBus 配置 ──
MESSAGE_BUS_TTL = int(os.getenv("MESSAGE_BUS_TTL", "1800"))         # 消息 TTL 秒数，0=不过期
MESSAGE_BUS_MAX_CAPACITY = int(os.getenv("MESSAGE_BUS_MAX_CAPACITY", "500"))
```

---

## 5. 典型场景

### 场景：迁移前后 route_node 写入 router_result

**迁移前**：
```python
# graph.py route_node 中
state["shared_memory"]["router_result"] = router_output
bus = _get_bus(state)
bus.publish("router", "*", MSG_ROUTER_RESULT, router_output)
_save_bus(state, bus)  # 内部又把 bus 同步回 shared_memory
```

**迁移后**：
```python
# graph.py route_node 中
bus = _get_bus(state)
bus.publish("router", "*", MSG_ROUTER_RESULT, router_output)
_save_bus(state, bus)
```

**其他节点读取**：
```python
# 迁移前：
router_result = state["shared_memory"].get("router_result")

# 迁移后：
bus = _get_bus(state)
msg = bus.get_latest(MSG_ROUTER_RESULT)
router_result = msg.payload if msg else None
```

---

## 6. 实施步骤

| 步骤 | 任务 | 交付物 | 状态 |
|------|------|--------|------|
| 1 | 全局搜索 `shared_memory` 的所有读写点，建立迁移清单（预计 30-40 处） | 迁移清单文档 | 🔲 |
| 2 | `core/constants.py` 新增标准消息类型常量 | `core/constants.py` | 🔲 |
| 3 | `core/message_bus.py` 增加 TTL + 容量限制，删除 `to_shared_memory` / `from_shared_memory` / `_LEGACY_KEY_MAP` | `core/message_bus.py` | 🔲 |
| 4 | `config/settings.py` 新增 `MESSAGE_BUS_TTL` / `MESSAGE_BUS_MAX_CAPACITY` | `config/settings.py` | 🔲 |
| 5 | `core/graph.py` 移除 `_save_bus` 中的 shared_memory 同步，逐节点将 `state["shared_memory"]` 读写改为 `bus.publish()` / `bus.query()` | `core/graph.py` | 🔲 |
| 6 | `core/task_executor.py` 移除 `_apply_task_outcome` 中的 shared_memory 写入 | `core/task_executor.py` | 🔲 |
| 7 | `core/router.py` 将 shared_memory 读取改为 bus.query() | `core/router.py` | 🔲 |
| 8 | `core/runtime.py` resume 标记迁移到 MessageBus | `core/runtime.py` | 🔲 |
| 9 | `agents/*.py` 逐文件迁移 shared_memory 读写 | `agents/` | 🔲 |
| 10 | `core/state.py` 移除 `shared_memory` 字段 | `core/state.py` | 🔲 |
| 11 | 全量回归测试 `pytest tests -q` | 测试报告 | 🔲 |
| 12 | 清理：搜索确认 `shared_memory` 零残留 | 验证报告 | 🔲 |

**注意**：步骤 5-9 建议逐节点迁移+测试，不要一次性全改。每迁移一个节点跑一次相关测试。

---

## 7. 风险与注意事项

| 风险 | 缓解措施 |
|------|---------|
| 遗漏某个 shared_memory 读写点导致运行时 KeyError | 步骤 1 全局搜索建立完整清单；步骤 12 零残留验证 |
| LangGraph checkpoint 格式变化（移除 shared_memory 字段） | checkpoint 兼容：加载旧 checkpoint 时忽略 shared_memory 字段，不报错 |
| TTL 过短导致跨节点消息过期 | 默认 30 分钟足够单次任务执行；Worker 长任务场景可调大 |
| resume 逻辑依赖 shared_memory 的 checkpoint 数据 | resume 代码同步改为从 MessageBus 读取；对旧 checkpoint 做 fallback 兼容 |
| 并发节点同时 publish 导致消息丢失 | MessageBus 已有 `threading.Lock`，并发安全 |
