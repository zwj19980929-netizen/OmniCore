# R7: Session Memory 后台提炼方案

> 优先级：P3 | 预估工作量：2-3 天 | 依赖：R1（需复用 snip 逻辑中的 session memory 保留机制） | 被依赖：无

---

## 1. 目标

当前会话没有"工作记忆"概念。R1 的 History Snip 只能丢信息，无法提炼关键状态。长会话中模型逐渐失去对早期决策的记忆——为什么选了这个方案、之前尝试过什么、哪些约束条件已经确认。

Claude Code 的做法：后台 fork 一个低权限 agent，定期（非每轮）提炼当前会话的工作记忆摘要，写入 session memory 文件。Compact 时优先保留 session memory 而非对话历史。

本方案为 OmniCore 实现类似机制。

**验收标准**：

- 长会话中每 `SESSION_MEMORY_INTERVAL` 轮（默认 8）自动提炼一次 session memory
- Session memory 写入 `data/sessions/{session_id}_memory.md`
- History snip 时 session memory 作为首条系统消息保留，不被裁剪
- 提炼过程不阻塞主执行流程（异步或在非关键路径上执行）
- 提炼使用低成本模型（`complexity=low`）
- 全量测试通过

---

## 2. 现有代码基础

| 组件 | 文件 | 当前状态 |
|------|------|---------|
| 会话管理 | `core/runtime.py` | `get_or_create_session()`，session 持久化到 `RuntimeStateStore` |
| 记忆系统 | `memory/` | ChromaDB 向量记忆（`scoped_chroma_store.py`）、知识库（`knowledge_store.py`）、Skill（`skill_store.py`）——但无会话级工作记忆 |
| 历史裁剪 | R1 `utils/context_budget.py` | `snip_history()` 裁剪历史消息 |
| LLM 调用 | `core/llm.py` | `LLMClient(complexity=...)` 支持复杂度路由 |
| 任务结果 | `core/runtime.py` `_finalize_runtime_result()` | finalize 阶段做记忆持久化、通知等 |

**关键发现**：

1. `_finalize_runtime_result()` 已经在做记忆持久化（`self._persist_memory()`），但是持久化的是任务结果到向量库，不是会话工作记忆
2. `snip_history()` 目前只做简单裁剪，没有"保留 session memory"的逻辑
3. `LLMClient(complexity="low")` 可以路由到低成本模型（DeepSeek/Gemini Flash），适合做提炼
4. 当前没有后台异步 LLM 调用的机制，需要设计非阻塞执行方式

---

## 3. 架构设计

### 3.1 Session Memory 在执行链路中的位置

```
用户输入
    │
    ▼
┌────────────────────┐
│  route_node        │
│  (turn_count += 1) │
└────────┬───────────┘
         │
         ▼
┌────────────────────────────────┐
│  session_memory_check          │ ← 在执行节点之间，检查是否需要提炼
│  if turn_count % interval == 0│
│    → 触发后台提炼              │
└────────┬───────────────────────┘
         │
         ▼
┌────────────────────┐
│  parallel_executor │
└────────┬───────────┘
         │
         ▼
   ... 后续节点 ...
         │
         ▼
┌────────────────────┐
│  finalize_node     │ ← 最后一次提炼（确保最终状态被捕获）
│  → 同步提炼        │
└────────────────────┘
```

### 3.2 Session Memory 内容结构

```markdown
# Session Memory — {session_id}

> 最后更新: 2026-04-01 14:30 (第 16 轮)

## 当前工作状态
- 用户目标：调研 3 个竞品网站并生成对比报告
- 已完成：竞品 A 和 B 的信息已抓取
- 进行中：竞品 C 的页面需要浏览器交互
- 阻塞项：无

## 关键决策记录
- 第 3 轮：选择 web.smart_extract 而非 browser.interact 抓取竞品 A（因为纯静态页面）
- 第 8 轮：竞品 B 需要登录，改用 browser.interact 并手动输入凭据

## 已确认的约束
- 用户要求输出格式为 Markdown 表格
- 对比维度：价格、功能覆盖、用户评价
- 不需要包含截图

## 尝试过但失败的路径
- 竞品 C 的 API 端点返回 403，需要浏览器方式
```

### 3.3 提炼触发策略

```
触发条件（全部 AND）：
1. SESSION_MEMORY_ENABLED = true
2. turn_count > 0 且 turn_count % SESSION_MEMORY_INTERVAL == 0
3. 距离上次提炼已过 MIN_EXTRACT_GAP 轮（防止重规划导致连续触发）
4. 当前没有工具正在执行（避免中间状态）

提炼输入：
- 最近 N 条对话消息（N = SESSION_MEMORY_INTERVAL * 2，覆盖两个周期）
- 当前 session memory（如果已存在）
- 当前 task_queue 状态

提炼输出：
- 更新后的 session memory（Markdown 格式）
- 写入 data/sessions/{session_id}_memory.md
```

---

## 4. 详细设计

### 4.1 新建 `core/session_memory.py`

```python
"""会话工作记忆：定期提炼、持久化、恢复"""

import os
import time
from datetime import datetime
from typing import Optional
from config.settings import (
    SESSION_MEMORY_ENABLED,
    SESSION_MEMORY_INTERVAL,
)


SESSIONS_DIR = os.path.join("data", "sessions")


class SessionMemoryManager:
    """管理单个会话的工作记忆"""

    def __init__(self, session_id: str, llm_client=None):
        self.session_id = session_id
        self._llm = llm_client
        self._last_extract_turn = 0
        self._memory_path = os.path.join(SESSIONS_DIR, f"{session_id}_memory.md")

    def should_extract(self, turn_count: int, is_tool_executing: bool = False) -> bool:
        """判断是否应该触发提炼"""
        if not SESSION_MEMORY_ENABLED:
            return False
        if is_tool_executing:
            return False
        if turn_count <= 0:
            return False
        if turn_count - self._last_extract_turn < SESSION_MEMORY_INTERVAL:
            return False
        return True

    def extract(self, messages: list, task_queue: list = None,
                turn_count: int = 0) -> str:
        """
        提炼 session memory。

        Args:
            messages: 最近的对话消息
            task_queue: 当前任务队列状态
            turn_count: 当前轮次

        Returns:
            提炼后的 session memory 文本
        """
        existing_memory = self.load()

        # 构建提炼 prompt
        prompt = self._build_extract_prompt(messages, task_queue, existing_memory, turn_count)

        # 使用低成本模型提炼
        if self._llm is None:
            from core.llm import LLMClient
            self._llm = LLMClient(complexity="low")

        response = self._llm.call(
            system_prompt=EXTRACT_SYSTEM_PROMPT,
            user_message=prompt,
        )

        memory_text = response.get("content", "") if isinstance(response, dict) else str(response)

        # 保存
        self.save(memory_text, turn_count)
        self._last_extract_turn = turn_count

        return memory_text

    def save(self, memory_text: str, turn_count: int = 0):
        """保存 session memory 到文件"""
        os.makedirs(SESSIONS_DIR, exist_ok=True)

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        header = (
            f"# Session Memory — {self.session_id}\n\n"
            f"> 最后更新: {now} (第 {turn_count} 轮)\n\n"
        )

        with open(self._memory_path, "w", encoding="utf-8") as f:
            f.write(header + memory_text)

    def load(self) -> str:
        """加载现有的 session memory"""
        if not os.path.exists(self._memory_path):
            return ""
        try:
            with open(self._memory_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""

    def _build_extract_prompt(self, messages: list, task_queue: list,
                              existing_memory: str, turn_count: int) -> str:
        """构建提炼 prompt"""
        parts = []

        # 现有 session memory
        if existing_memory:
            parts.append(f"## 现有工作记忆\n\n{existing_memory}")

        # 最近的对话消息
        recent_messages = messages[-(SESSION_MEMORY_INTERVAL * 2):]
        msg_text = self._format_messages(recent_messages)
        parts.append(f"## 最近对话（第 {turn_count - len(recent_messages) + 1}~{turn_count} 轮）\n\n{msg_text}")

        # 当前任务状态
        if task_queue:
            task_summary = self._format_task_queue(task_queue)
            parts.append(f"## 当前任务状态\n\n{task_summary}")

        return "\n\n---\n\n".join(parts)

    def _format_messages(self, messages: list) -> str:
        """将消息列表格式化为文本"""
        lines = []
        for msg in messages:
            if hasattr(msg, "type") and hasattr(msg, "content"):
                role = msg.type
                content = msg.content
            elif isinstance(msg, dict):
                role = msg.get("role", msg.get("type", "?"))
                content = msg.get("content", "")
            else:
                continue

            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )

            # 截断长消息
            if isinstance(content, str) and len(content) > 500:
                content = content[:500] + "..."

            lines.append(f"[{role}] {content}")

        return "\n".join(lines)

    def _format_task_queue(self, task_queue: list) -> str:
        """将任务队列格式化为文本"""
        lines = []
        for task in task_queue:
            tid = task.get("task_id", "?")
            status = task.get("status", "?")
            tool = task.get("tool_name", task.get("task_type", "?"))
            desc = str(task.get("params", {}).get("description", ""))[:80]
            lines.append(f"- [{status}] {tid}: {desc} ({tool})")
        return "\n".join(lines)


# 提炼用的 system prompt（静态，可缓存）
EXTRACT_SYSTEM_PROMPT = """你是一个工作记忆提炼助手。你的任务是从对话历史和任务状态中提炼出关键的工作记忆。

输出格式要求：
1. **当前工作状态**：用户目标、已完成的内容、进行中的内容、阻塞项
2. **关键决策记录**：在哪一轮做了什么决策、为什么
3. **已确认的约束**：用户明确提出的要求和限制
4. **尝试过但失败的路径**：避免重复尝试

要求：
- 简洁，每个条目不超过一行
- 只保留对后续执行有用的信息
- 如果有现有工作记忆，在其基础上更新（增量更新，不是重写）
- 已完成的任务如果不影响后续决策，可以精简描述
- 使用 Markdown 格式输出"""
```

### 4.2 修改 `utils/context_budget.py` — snip 时保留 session memory

```python
def snip_history(
    messages: list,
    max_messages: int = None,
    keep_recent: int = None,
    summary_max_chars: int = 200,
    session_memory: str = "",       # ← 新增参数
) -> list:
    """
    裁剪历史消息列表。

    如果提供了 session_memory，将其作为第一条 SystemMessage 插入，
    确保 LLM 始终能看到工作记忆。
    """
    if max_messages is None:
        max_messages = HISTORY_MAX_MESSAGES
    if keep_recent is None:
        keep_recent = HISTORY_KEEP_RECENT

    result = []

    # 插入 session memory 作为首条消息
    if session_memory:
        from langchain_core.messages import SystemMessage
        result.append(SystemMessage(
            content=f"[工作记忆 — 以下是当前会话的关键状态摘要]\n\n{session_memory}"
        ))

    if len(messages) <= max_messages:
        return result + messages

    # 保留最近 keep_recent 条完整
    recent = messages[-keep_recent:]
    older = messages[:-keep_recent]

    # 更早的消息只保留摘要
    snipped = []
    for msg in older:
        snipped.append(_summarize_message(msg, summary_max_chars))

    return result + snipped + recent
```

### 4.3 在 graph 中集成 session memory 提炼

```python
# 在 parallel_executor_node（或 R3 拆分后的 graph_nodes.py）中

from core.session_memory import SessionMemoryManager

def parallel_executor_node(state):
    loop = LoopState.from_dict(state.get("loop_state", {}))
    session_id = state.get("session_id", "default")

    # 检查是否需要提炼 session memory
    sm_manager = SessionMemoryManager(session_id)
    if sm_manager.should_extract(loop.turn_count):
        # 在非关键路径上执行（不阻塞工具执行）
        try:
            sm_manager.extract(
                messages=state.get("messages", []),
                task_queue=state.get("task_queue", []),
                turn_count=loop.turn_count,
            )
        except Exception:
            pass  # 提炼失败不影响主流程

    # ... 现有执行逻辑 ...
    return state
```

### 4.4 在 LLM 调用时注入 session memory

```python
# 在 route_node / replanner_node / finalize_node 中

from core.session_memory import SessionMemoryManager
from utils.context_budget import snip_history

def route_node(state):
    session_id = state.get("session_id", "default")
    sm_manager = SessionMemoryManager(session_id)
    session_memory = sm_manager.load()

    # 裁剪历史时保留 session memory
    conversation_history = snip_history(
        state.get("messages", []),
        session_memory=session_memory,
    )

    # ... 传给 LLM ...
```

### 4.5 在 finalize 中做最终提炼

```python
# finalize_node 中

def finalize_node(state):
    # ... 现有 finalize 逻辑 ...

    # 最终一次 session memory 提炼（同步执行，确保最终状态被捕获）
    session_id = state.get("session_id", "default")
    sm_manager = SessionMemoryManager(session_id)
    loop = LoopState.from_dict(state.get("loop_state", {}))

    try:
        sm_manager.extract(
            messages=state.get("messages", []),
            task_queue=state.get("task_queue", []),
            turn_count=loop.turn_count,
        )
    except Exception:
        pass

    return state
```

### 4.6 新建 `prompts/session_memory_extract.txt`

```
你是一个工作记忆提炼助手。你的任务是从对话历史和任务状态中提炼出关键的工作记忆。

输出格式要求：
1. **当前工作状态**：用户目标、已完成的内容、进行中的内容、阻塞项
2. **关键决策记录**：在哪一轮做了什么决策、为什么
3. **已确认的约束**：用户明确提出的要求和限制
4. **尝试过但失败的路径**：避免重复尝试

要求：
- 简洁，每个条目不超过一行
- 只保留对后续执行有用的信息
- 如果有现有工作记忆，在其基础上更新（增量更新，不是重写）
- 已完成的任务如果不影响后续决策，可以精简描述
```

### 4.7 修改 `config/settings.py`

```python
# ── Session Memory 配置 ──
SESSION_MEMORY_ENABLED = os.getenv("SESSION_MEMORY_ENABLED", "false").lower() == "true"
SESSION_MEMORY_INTERVAL = int(os.getenv("SESSION_MEMORY_INTERVAL", "8"))
```

---

## 5. 典型场景

### 场景：20 轮长会话中的 session memory 演变

```
轮次 1-7: 正常执行，无 session memory
  → 用户要求调研竞品
  → route_node 规划 5 个任务
  → 任务 1-2 完成

轮次 8: 触发首次提炼
  → SessionMemoryManager.extract()
  → 生成:
    当前工作状态: 用户目标调研 3 竞品，2/5 任务完成
    关键决策: 选择 web.smart_extract 抓取竞品A
    已确认约束: 输出 Markdown 表格

轮次 9-15: 继续执行
  → 任务 3 失败，触发重规划
  → 新方案改用 browser.interact

轮次 16: 触发第二次提炼
  → 在首次 memory 基础上增量更新:
    当前工作状态: 4/5 任务完成（重规划后）
    新增决策: 第 12 轮从 web 切换到 browser（页面需要 JS 渲染）
    新增失败路径: web.smart_extract 无法处理动态加载页面

轮次 17: history snip 触发
  → 消息总数 > 20
  → snip_history(session_memory=<第二次提炼的内容>)
  → LLM 看到: [工作记忆摘要] + [最近 10 条完整消息]
  → 模型仍然知道: 为什么切换到 browser、用户要 Markdown 表格

轮次 20: finalize
  → 最终提炼
  → session memory 标记为 completed
```

**效果**：轮次 17 的 LLM 调用中，虽然早期消息被裁剪，但通过 session memory 仍然保留了：用户目标、关键决策原因、约束条件、失败路径。

---

## 6. 实施步骤

| 步骤 | 任务 | 交付物 | 状态 |
|------|------|--------|------|
| 1 | 新建 `core/session_memory.py`（SessionMemoryManager 核心类） | `core/session_memory.py` | 🔲 |
| 2 | 新建 `prompts/session_memory_extract.txt`（提炼 prompt） | `prompts/session_memory_extract.txt` | 🔲 |
| 3 | `config/settings.py` 新增 `SESSION_MEMORY_ENABLED` / `SESSION_MEMORY_INTERVAL` | `config/settings.py` | 🔲 |
| 4 | 修改 `utils/context_budget.py` `snip_history()` 支持 `session_memory` 参数 | `utils/context_budget.py` | 🔲 |
| 5 | 在 parallel_executor_node 中集成提炼触发检查 | `core/graph.py` 或 `core/graph_nodes.py` | 🔲 |
| 6 | 在 route_node / replanner_node / finalize_node 的 LLM 调用中注入 session memory | 各节点文件 | 🔲 |
| 7 | finalize_node 中执行最终提炼 | `core/graph.py` 或 `core/finalizer.py` | 🔲 |
| 8 | 单元测试：提炼触发逻辑、session memory 保存/加载、snip 集成 | `tests/test_session_memory_unit.py` | 🔲 |

---

## 7. 风险与注意事项

| 风险 | 缓解措施 |
|------|---------|
| 提炼 LLM 调用增加额外成本 | 使用 `complexity=low` 路由到低成本模型；每 8 轮才触发一次；输入限制在最近 16 条消息 |
| 提炼质量不稳定（低成本模型能力有限） | 提炼 prompt 结构化、输出格式固定；提炼结果只做增量更新，不重写 |
| 提炼耗时阻塞主流程 | 在非关键路径触发（executor 前，不阻塞工具执行）；失败静默跳过 |
| session memory 文件越积越多 | 默认 `SESSION_MEMORY_ENABLED=false`，需手动开启；可配合 session 过期策略清理 |
| session memory 内容与实际 state 不一致 | session memory 是辅助上下文，不参与执行决策；task_queue 仍是权威来源 |
| 首次会话无 session memory 导致体验不一致 | 无 session memory 时 snip_history 行为不变（退化为纯裁剪） |
