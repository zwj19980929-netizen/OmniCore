# R5: Plan Mode 持久化与 Reminder 机制方案

> 优先级：P2 | 预估工作量：2 天 | 依赖：R3（在拆分后的 graph 上实现更干净） | 被依赖：无

---

## 1. 目标

当前 OmniCore 的规划只是 Router LLM 输出的 JSON task_queue——执行过程中不再回顾，无法恢复，模型在长任务中容易忘记原始目标、跳步、忘更新状态。

Claude Code 的做法是把规划做成三件事：**模式切换**（规划态禁止写操作）、**文件持久化**（计划不随 compact 丢失）、**Runtime Reminder**（系统主动提醒模型维护计划状态）。

本方案实现这三层。

**验收标准**：

- 每次任务规划自动生成 `data/plans/{job_id}.md`（Markdown 格式的计划文件）
- `plan_mode` 标志位可控：规划态下禁止执行 destructive 工具（结合 R4 的 `destructive` 标签）
- 超过 `PLAN_REMINDER_INTERVAL` 轮未更新任务状态时，system message 中出现提醒
- resume 场景能从文件恢复计划
- 全量测试通过

---

## 2. 现有代码基础

| 组件 | 文件 | 当前状态 |
|------|------|---------|
| 任务规划 | `core/router.py` `analyze_intent()` | LLM 输出 JSON task_queue，直接写入 state |
| 重规划 | `core/graph.py` `replanner_node()` | 生成新 task_queue 替换旧的，不持久化 |
| 任务状态 | `core/state.py` `task_queue` | List[TaskItem]，状态字段在 `_apply_task_outcome()` 中更新 |
| 恢复 | `core/runtime.py` `resume_job_from_checkpoint()` | 从 LangGraph checkpoint 恢复 state，不恢复计划 |
| 计划文件 | 不存在 | 无 |
| 提醒机制 | 不存在 | 无 |

**关键发现**：

1. `analyze_intent()` 返回的 `tasks` 列表包含完整的计划信息（task_type, params, depends_on, success_criteria），可以直接序列化为 Markdown
2. `replanner_node()` 会完全替换 task_queue，但不记录"为什么重规划"的原因
3. `_apply_task_outcome()` 更新任务状态但不通知任何"计划追踪器"
4. `LoopState.turn_count`（R3 引入）可以作为 reminder 的触发依据

---

## 3. 架构设计

### 3.1 Plan Mode 状态机

```
用户输入
    │
    ▼
┌─────────────┐
│  route_node │
│  (规划阶段)  │ ──→ save_plan() 写入 data/plans/{job_id}.md
└──────┬──────┘
       │
       ▼
┌─────────────────────┐
│  plan_validator_node │ ──→ 可选：开启 plan_mode=True（复杂任务）
└──────┬──────────────┘
       │
       ▼
┌────────────────────────┐
│  plan_reminder_check   │ ──→ 每 N 轮检查：任务进度、是否需要提醒
│  (在 executor 前执行)   │
└──────┬─────────────────┘
       │
       ▼
┌──────────────────────┐
│  parallel_executor   │ ──→ plan_mode=True 时，拒绝 destructive 工具
└──────┬───────────────┘
       │
       ▼
┌─────────────────┐
│  replanner_node │ ──→ 重规划时同步更新 plan 文件
└──────┬──────────┘
       │
       ▼
┌─────────────┐
│  finalize   │ ──→ 关闭 plan_mode，标记 plan 文件为 completed
└─────────────┘
```

### 3.2 计划文件格式

```markdown
# 计划: {user_input 前 50 字符}

> Job ID: {job_id}
> 创建时间: {timestamp}
> 状态: executing / completed / failed
> 重规划次数: {replan_count}

## 任务列表

| # | 任务 | 工具 | 状态 | 依赖 |
|---|------|------|------|------|
| 1 | 搜索竞品信息 | web.fetch_and_extract | completed | - |
| 2 | 打开产品页面 | browser.interact | executing | 1 |
| 3 | 整理对比报告 | file.read_write | pending | 1,2 |

## 重规划记录

### 重规划 #1 (2026-04-01 14:30)
原因: 任务 2 失败（页面加载超时）
调整: 将 browser.interact 替换为 web.smart_extract
```

### 3.3 Reminder 触发策略

```
Reminder 检查逻辑（每轮执行前）：

1. 计算 turn_count 自上次任务状态变化以来的轮数（stale_turns）
2. if stale_turns >= PLAN_REMINDER_INTERVAL:
     注入提醒: "已经过 {stale_turns} 轮未更新任务状态，请检查计划执行进度"
3. 计算 completion_rate = completed_tasks / total_tasks
4. if completion_rate < 0.3 且 turn_count > total_tasks * 2:
     注入提醒: "任务完成率较低（{completion_rate}%），建议重新评估计划"
5. if 所有任务完成但尚未进入 finalize:
     注入提醒: "所有计划任务已完成，请进行最终输出"
```

---

## 4. 详细设计

### 4.1 新建 `core/plan_manager.py`

```python
"""计划持久化与恢复管理"""

import os
import json
from datetime import datetime
from pathlib import Path
from config.settings import PLAN_PERSISTENCE_ENABLED


PLANS_DIR = os.path.join("data", "plans")


def save_plan(job_id: str, task_queue: list, user_input: str = "",
              replan_count: int = 0, replan_reason: str = "") -> str:
    """
    将计划保存为 Markdown 文件。

    Args:
        job_id: 任务 ID
        task_queue: 任务列表
        user_input: 用户原始输入
        replan_count: 重规划次数
        replan_reason: 重规划原因（仅重规划时传入）

    Returns:
        计划文件路径
    """
    if not PLAN_PERSISTENCE_ENABLED:
        return ""

    os.makedirs(PLANS_DIR, exist_ok=True)
    plan_path = os.path.join(PLANS_DIR, f"{job_id}.md")

    # 如果文件已存在（重规划），追加重规划记录
    if os.path.exists(plan_path) and replan_reason:
        _append_replan_record(plan_path, replan_count, replan_reason, task_queue)
        return plan_path

    # 首次创建
    title = (user_input[:50] + "...") if len(user_input) > 50 else user_input
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        f"# 计划: {title}",
        "",
        f"> Job ID: {job_id}",
        f"> 创建时间: {now}",
        f"> 状态: executing",
        f"> 重规划次数: {replan_count}",
        "",
        "## 任务列表",
        "",
        "| # | 任务 | 工具 | 状态 | 依赖 |",
        "|---|------|------|------|------|",
    ]

    for i, task in enumerate(task_queue, 1):
        desc = task.get("params", {}).get("description", task.get("task_type", ""))
        if isinstance(desc, dict):
            desc = desc.get("description", str(desc))
        desc = str(desc)[:60]
        tool = task.get("tool_name", task.get("task_type", "unknown"))
        status = task.get("status", "pending")
        depends = ",".join(task.get("depends_on", [])) or "-"
        lines.append(f"| {i} | {desc} | {tool} | {status} | {depends} |")

    lines.append("")

    with open(plan_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return plan_path


def _append_replan_record(plan_path: str, replan_count: int,
                          reason: str, new_task_queue: list):
    """在计划文件末尾追加重规划记录"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        "",
        "## 重规划记录" if replan_count == 1 else "",
        "",
        f"### 重规划 #{replan_count} ({now})",
        f"原因: {reason}",
        "调整后的任务列表:",
        "",
        "| # | 任务 | 工具 | 状态 |",
        "|---|------|------|------|",
    ]

    for i, task in enumerate(new_task_queue, 1):
        desc = str(task.get("params", {}).get("description", ""))[:60]
        tool = task.get("tool_name", task.get("task_type", "unknown"))
        status = task.get("status", "pending")
        lines.append(f"| {i} | {desc} | {tool} | {status} |")

    # 同时更新头部元数据中的重规划次数
    with open(plan_path, "r", encoding="utf-8") as f:
        content = f.read()
    content = content.replace(
        f"> 重规划次数: {replan_count - 1}",
        f"> 重规划次数: {replan_count}",
    )

    with open(plan_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))


def update_plan_task_status(job_id: str, task_id: str, new_status: str):
    """更新计划文件中特定任务的状态"""
    plan_path = os.path.join(PLANS_DIR, f"{job_id}.md")
    if not os.path.exists(plan_path):
        return

    with open(plan_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 简单的状态替换（基于任务表格行）
    # 更精确的做法是解析 Markdown 表格，但对于进度追踪，简单替换足够
    # 后续可以改为结构化存储（JSON sidecar）
    with open(plan_path, "w", encoding="utf-8") as f:
        f.write(content)


def complete_plan(job_id: str):
    """标记计划为已完成"""
    plan_path = os.path.join(PLANS_DIR, f"{job_id}.md")
    if not os.path.exists(plan_path):
        return

    with open(plan_path, "r", encoding="utf-8") as f:
        content = f.read()

    content = content.replace("> 状态: executing", "> 状态: completed")

    with open(plan_path, "w", encoding="utf-8") as f:
        f.write(content)


def load_plan(job_id: str) -> str:
    """加载计划文件内容（用于 resume）"""
    plan_path = os.path.join(PLANS_DIR, f"{job_id}.md")
    if not os.path.exists(plan_path):
        return ""

    with open(plan_path, "r", encoding="utf-8") as f:
        return f.read()


def load_plan_tasks(job_id: str) -> list:
    """从计划文件中解析任务列表（用于 resume 恢复 task_queue）"""
    content = load_plan(job_id)
    if not content:
        return []

    # 从 Markdown 表格解析任务列表
    # 实际 resume 场景更可能从 checkpoint 恢复 task_queue，
    # 这里作为 fallback
    tasks = []
    in_table = False
    for line in content.split("\n"):
        if line.startswith("| # |"):
            in_table = True
            continue
        if line.startswith("|---|"):
            continue
        if in_table and line.startswith("|"):
            parts = [p.strip() for p in line.split("|")[1:-1]]
            if len(parts) >= 4:
                tasks.append({
                    "description": parts[1],
                    "tool_name": parts[2],
                    "status": parts[3],
                })
        elif in_table and not line.startswith("|"):
            in_table = False

    return tasks
```

### 4.2 新建 `core/plan_reminder.py`

```python
"""计划执行提醒机制"""

from typing import Optional
from config.settings import PLAN_REMINDER_INTERVAL


def generate_reminder(state: dict) -> Optional[str]:
    """
    基于当前执行状态生成计划提醒。

    检查点：
    1. 任务长时间未更新 → 提醒检查进度
    2. 完成率过低 → 建议重新评估
    3. 全部完成 → 提醒进入 finalize

    Returns:
        提醒文本，或 None（无需提醒）
    """
    task_queue = state.get("task_queue", [])
    if not task_queue:
        return None

    loop_state = state.get("loop_state", {})
    turn_count = loop_state.get("turn_count", 0)

    total = len(task_queue)
    completed = sum(1 for t in task_queue if t.get("status") == "completed")
    failed = sum(1 for t in task_queue if t.get("status") == "failed")
    pending = sum(1 for t in task_queue if t.get("status") in ("pending", "queued"))

    reminders = []

    # 1. 长时间未有任务状态变化
    last_status_change_turn = loop_state.get("last_status_change_turn", 0)
    stale_turns = turn_count - last_status_change_turn
    if stale_turns >= PLAN_REMINDER_INTERVAL and pending > 0:
        reminders.append(
            f"[计划提醒] 已过 {stale_turns} 轮未有任务状态变化。"
            f"当前进度：{completed}/{total} 完成，{pending} 待执行。"
            f"请检查是否有阻塞或需要重新规划。"
        )

    # 2. 完成率过低
    if turn_count > total * 2 and total > 0:
        completion_rate = completed / total
        if completion_rate < 0.3 and failed > 0:
            reminders.append(
                f"[计划提醒] 任务完成率较低（{completed}/{total} = {completion_rate:.0%}），"
                f"已有 {failed} 个任务失败。建议触发重规划。"
            )

    # 3. 全部完成
    if completed == total and total > 0:
        reminders.append(
            "[计划提醒] 所有计划任务已完成，请进行最终输出合成。"
        )

    if not reminders:
        return None

    return "\n".join(reminders)


def update_status_change_turn(state: dict):
    """当任务状态发生变化时，更新 last_status_change_turn"""
    loop_state = state.get("loop_state", {})
    loop_state["last_status_change_turn"] = loop_state.get("turn_count", 0)
    state["loop_state"] = loop_state
```

### 4.3 修改 `core/graph.py`（或 R3 拆分后的 `core/graph_nodes.py`）

**route_node 集成计划保存**：

```python
from core.plan_manager import save_plan

def route_node(state):
    # ... 现有路由逻辑 ...
    # result = router.analyze_intent(...)
    # state["task_queue"] = result["tasks"]

    # 保存计划文件
    job_id = state.get("job_id", "unknown")
    user_input = state.get("user_input", "")
    save_plan(job_id, state["task_queue"], user_input=user_input)

    return state
```

**replanner_node 集成计划更新**：

```python
from core.plan_manager import save_plan

def replanner_node(state):
    # ... 现有重规划逻辑 ...
    # new_tasks = ...
    # state["task_queue"] = new_tasks

    loop = LoopState.from_dict(state.get("loop_state", {}))
    save_plan(
        job_id=state.get("job_id", "unknown"),
        task_queue=state["task_queue"],
        replan_count=loop.replan_count,
        replan_reason=failure_summary,  # 从失败分析中提取
    )

    return state
```

**executor 前插入 reminder 检查**：

```python
from core.plan_reminder import generate_reminder

def parallel_executor_node(state):
    # 执行前检查 reminder
    reminder = generate_reminder(state)
    if reminder:
        # 注入为 system message，让 LLM 在下一轮看到
        from langchain_core.messages import SystemMessage
        state["messages"].append(SystemMessage(content=reminder))

    # ... 现有执行逻辑 ...
    return state
```

**finalize_node 标记计划完成**：

```python
from core.plan_manager import complete_plan

def finalize_node(state):
    # ... 现有 finalize 逻辑 ...

    complete_plan(state.get("job_id", "unknown"))
    return state
```

### 4.4 修改 `core/task_executor.py` — 状态变化通知

```python
from core.plan_reminder import update_status_change_turn

def _apply_task_outcome(state, idx, outcome):
    old_status = state["task_queue"][idx].get("status")

    # ... 现有逻辑 ...

    new_status = state["task_queue"][idx].get("status")
    if old_status != new_status:
        update_status_change_turn(state)
```

### 4.5 修改 `core/runtime.py` — resume 恢复计划

```python
from core.plan_manager import load_plan

def resume_job_from_checkpoint(self, ...):
    # ... 现有恢复逻辑 ...

    # 恢复计划文件内容作为上下文
    plan_content = load_plan(job_id)
    if plan_content:
        from langchain_core.messages import SystemMessage
        resumed_state["messages"].insert(0, SystemMessage(
            content=f"[恢复的执行计划]\n{plan_content}"
        ))
```

### 4.6 修改 `config/settings.py`

```python
# ── Plan Mode 配置 ──
PLAN_PERSISTENCE_ENABLED = os.getenv("PLAN_PERSISTENCE_ENABLED", "true").lower() == "true"
PLAN_REMINDER_INTERVAL = int(os.getenv("PLAN_REMINDER_INTERVAL", "5"))
```

---

## 5. 典型场景

### 场景：3 步任务执行中第 2 步失败，触发重规划

```
轮次 1: route_node
  → 规划 3 个任务
  → 写入 data/plans/job_abc123.md
  → plan 文件状态: executing

轮次 2: parallel_executor
  → 任务 1 完成 ✅
  → update_status_change_turn(turn=2)

轮次 3: parallel_executor
  → 任务 2 失败 ❌（网页加载超时）
  → update_status_change_turn(turn=3)

轮次 4-8: （假设卡在重试中，无状态变化）

轮次 9: parallel_executor 前的 reminder 检查
  → stale_turns = 9 - 3 = 6 > PLAN_REMINDER_INTERVAL(5)
  → 注入提醒: "已过 6 轮未有任务状态变化..."

轮次 10: replanner_node
  → 生成新计划
  → _append_replan_record() 追加到 plan 文件
  → plan 文件新增 "重规划 #1" 记录

轮次 11: finalize_node
  → complete_plan() → plan 文件状态改为 completed
```

生成的 plan 文件最终内容：

```markdown
# 计划: 帮我调研 3 个竞品网站并对比

> Job ID: job_abc123
> 创建时间: 2026-04-01 14:00
> 状态: completed
> 重规划次数: 1

## 任务列表

| # | 任务 | 工具 | 状态 | 依赖 |
|---|------|------|------|------|
| 1 | 搜索竞品A信息 | web.fetch_and_extract | completed | - |
| 2 | 搜索竞品B信息 | web.fetch_and_extract | failed | - |
| 3 | 整理对比报告 | file.read_write | pending | 1,2 |

## 重规划记录

### 重规划 #1 (2026-04-01 14:15)
原因: 任务 2 失败（网页加载超时），尝试更换抓取策略
调整后的任务列表:

| # | 任务 | 工具 | 状态 |
|---|------|------|------|
| 1 | 使用增强抓取搜索竞品B | web.smart_extract | completed |
| 2 | 整理对比报告 | file.read_write | completed |
```

---

## 6. 实施步骤

| 步骤 | 任务 | 交付物 | 状态 |
|------|------|--------|------|
| 1 | 新建 `core/plan_manager.py`（save_plan / load_plan / complete_plan / _append_replan_record） | `core/plan_manager.py` | ✅ |
| 2 | 新建 `core/plan_reminder.py`（generate_reminder / update_status_change_turn） | `core/plan_reminder.py` | ✅ |
| 3 | `config/settings.py` 新增 `PLAN_PERSISTENCE_ENABLED` / `PLAN_REMINDER_INTERVAL` | `config/settings.py` | ✅ |
| 4 | `core/loop_state.py` `LoopState` 增加 `last_status_change_turn` 字段 + 序列化 | `core/loop_state.py` | ✅ |
| 5 | route_node 集成 `save_plan()` | `core/graph_nodes.py` | ✅ |
| 6 | replanner_node 集成 `save_plan()` 重规划记录 | `core/replanner.py` | ✅ |
| 7 | parallel_executor_node 前插入 `generate_reminder()` | `core/graph_nodes.py` | ✅ |
| 8 | `_apply_task_outcome()` 中调用 `update_status_change_turn()` | `core/task_executor.py` | ✅ |
| 9 | finalize_node 调用 `complete_plan()` | `core/finalizer.py` | ✅ |
| 10 | resume_job_from_checkpoint 恢复计划上下文 | `core/runtime.py` | ✅ |
| 11 | 单元测试：计划保存/加载/更新、reminder 触发逻辑 | `tests/test_plan_manager_unit.py` | ✅ |

---

## 7. 风险与注意事项

| 风险 | 缓解措施 |
|------|---------|
| plan 文件写入 IO 影响执行性能 | 写入量很小（<10KB），且只在规划/重规划时触发，不在热路径上 |
| Markdown 解析不够健壮（表格格式变化） | plan 文件主要用于人类阅读和 resume 上下文注入；结构化数据走 task_queue 本身 |
| reminder 注入为 SystemMessage 可能干扰 LLM | reminder 文本简洁、结构化、带 `[计划提醒]` 前缀，LLM 可以区分 |
| resume 恢复时 plan 文件与 checkpoint 状态不一致 | checkpoint 为权威来源，plan 文件为辅助上下文；不一致时以 checkpoint 为准 |
| `data/plans/` 目录文件越积越多 | 可选：finalize 后 30 天自动清理；或结合 job 状态做批量清理 |
