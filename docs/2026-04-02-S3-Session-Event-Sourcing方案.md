# S3: Session Event Sourcing 方案

> 所属规划：[Claude Code 启发架构演进规划](2026-04-02-Claude-Code启发架构演进规划.md)
>
> 优先级：P1 | 预估工作量：3-4 天 | 状态：🔲 未开始
>
> 前置依赖：无（独立替换现有持久化层）

---

## 问题分析

当前 session/job 状态通过 `runtime_state_store.py` 以 mutable snapshot 方式持久化：

1. **覆写式持久化**：每次状态变更覆写整个文件，无法追溯历史变更
2. **无审计日志**：不知道一个 job 经历了哪些状态跳转、在哪一步失败、重试了几次
3. **并发不安全**：多个 worker 同时更新同一 job 可能互相覆盖
4. **断点续传脆弱**：snapshot 损坏 = 整个 session 丢失，无增量恢复能力
5. **checkpoint 与 state 分离**：`resume_job_from_checkpoint()` 需要从多处拼凑恢复数据

## Claude Code 参考实现

```
// Append-only JSONL: {sessionId}.jsonl
// 写路径极简：entries queue in memory → drainWriteQueue() batch flush
// 读路径做重建：conversation graph reconstruction, snip removal, orphan recovery
// Metadata re-append to tail for fast list loading
// Subagent sidechain: agent-{id}.jsonl in subagents/ subdirectory
// isTranscriptMessage() 严格分类，progress 不混入 transcript
```

## 详细实现步骤

### S3-1: Event 数据模型

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | 新建 `core/event_log.py`，定义 `SessionEvent` dataclass：`event_id: str`(UUID)、`session_id: str`、`job_id: Optional[str]`、`event_type: str`、`timestamp: str`(ISO)、`data: dict`、`parent_event_id: Optional[str]` | `core/event_log.py`（新建） | 🔲 |
| 2 | 定义 event_type 枚举：`session_start`、`job_submitted`、`job_status_changed`、`task_started`、`task_completed`、`task_failed`、`plan_created`、`plan_updated`、`artifact_created`、`approval_requested`、`approval_resolved`、`session_end`、`metadata_updated` | 同上 | 🔲 |
| 3 | 实现 `SessionEvent.to_jsonl() -> str` 和 `SessionEvent.from_jsonl(line) -> SessionEvent` | 同上 | 🔲 |

### S3-2: EventWriter（写路径）

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | 在 `core/event_log.py` 实现 `EventWriter` 类：内存队列 + `flush()` 批量写入 | `core/event_log.py` | 🔲 |
| 2 | `append(event)` → 加入内存队列；`flush()` → 批量 append 到 `data/events/{session_id}.jsonl` | 同上 | 🔲 |
| 3 | 自动 flush 策略：队列达到 `SESSION_EVENT_FLUSH_INTERVAL`（默认 5 秒）或队列满 50 条时触发 | 同上 | 🔲 |
| 4 | event_id UUID 去重：`event_set` 内存集合，防止同一 event 重复写入 | 同上 | 🔲 |
| 5 | 文件权限固定 `0o600`（session 日志包含用户数据） | 同上 | 🔲 |

### S3-3: EventReader（读/恢复路径）

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | 在 `core/event_log.py` 实现 `EventReader` 类：`load_session(session_id) -> list[SessionEvent]` | `core/event_log.py` | 🔲 |
| 2 | `rebuild_job_state(events, job_id) -> dict`：从 event 序列重建 job 最终状态 | 同上 | 🔲 |
| 3 | `rebuild_conversation(events, job_id) -> list[Message]`：从 event 序列重建对话历史 | 同上 | 🔲 |
| 4 | 损坏行容错：parse 失败时跳过该行并记 warning（不因单行损坏丢失整个 session） | 同上 | 🔲 |
| 5 | Metadata 快速扫描：`scan_session_metadata(session_id) -> dict`，只读文件头 + 尾各 64KB 提取元数据 | 同上 | 🔲 |

### S3-4: 集成到执行循环

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | `core/runtime.py`：`submit_job()` 时写 `job_submitted` event | `core/runtime.py` | 🔲 |
| 2 | `core/graph_nodes.py`：各节点状态变更时写 `task_started` / `task_completed` / `task_failed` event | `core/graph_nodes.py` | 🔲 |
| 3 | `core/plan_manager.py`：plan 变更时写 `plan_created` / `plan_updated` event | `core/plan_manager.py` | 🔲 |
| 4 | `core/policy_engine.py`：审批事件写 `approval_requested` / `approval_resolved` event | `core/policy_engine.py` | 🔲 |
| 5 | `utils/artifact_store.py`：artifact 创建时写 `artifact_created` event | `utils/artifact_store.py` | 🔲 |

### S3-5: Resume 基于 Event Log

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | `core/runtime.py`：`resume_job_from_checkpoint()` 改为从 event log 重建状态 | `core/runtime.py` | 🔲 |
| 2 | 保留原 checkpoint 机制作为 fallback：event log 不存在时走旧路径 | 同上 | 🔲 |
| 3 | Resume 时检测最后一个 event 是否是中断（无 `session_end`），注入 `Continue from where you left off.` | 同上 | 🔲 |

### S3-6: 配置与测试

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | 配置项 `SESSION_EVENT_LOG_ENABLED`、`SESSION_EVENT_FLUSH_INTERVAL` | `config/settings.py` | 🔲 |
| 2 | EventWriter 单元测试（append、flush、去重、文件权限） | `tests/test_event_log_unit.py`（新建） | 🔲 |
| 3 | EventReader 单元测试（重建状态、重建对话、损坏容错、metadata 扫描） | 同上 | 🔲 |
| 4 | 集成测试（submit → execute → complete 全流程 event 验证） | `tests/test_event_log_integration.py`（新建） | 🔲 |
| 5 | 全量回归 | `pytest tests -q` | 🔲 |

---

## 核心设计决策

1. **渐进式迁移**：`SESSION_EVENT_LOG_ENABLED` 默认 false，开启后 event log 与原 snapshot 双写。稳定后再切为 event log 主路径
2. **写简读复**：写路径只做 append + flush，所有重建逻辑在读路径。写路径的简单性保证了运行时性能
3. **event 粒度**：以"状态变更"为粒度，不记录中间过程（如 LLM streaming token）。对比 Claude Code 的 `isTranscriptMessage()` 分类
4. **不替换 LangGraph state**：event log 是额外的审计/恢复层，不替代 `OmniCoreState` 的内存状态管理
5. **文件布局**：`data/events/{session_id}.jsonl`，与现有 `data/sessions/` 分开，便于独立管理生命周期

## 验收标准

- 一个 job 从提交到完成的所有状态变更都有 event 记录
- 从 event log 重建的 job 状态与 snapshot 一致
- 单行 JSONL 损坏不影响其余 event 的恢复
- Resume 场景能从 event log 正确恢复到中断点
- 写操作延迟 < 1ms（内存队列 + 异步 flush）
