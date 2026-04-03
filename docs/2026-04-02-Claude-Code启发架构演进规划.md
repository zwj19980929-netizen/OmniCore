# OmniCore 架构演进规划（Claude Code 启发）

> 编写时间：2026-04-02
>
> 灵感来源：Claude Code 源码深度分析（见 `/Users/zhangwenjun/zwj_project/claude-code-analysis/`）
>
> 前置：本文档在 `2026-04-01-Runtime架构优化规划.md`（R1-R7）基础上，识别**下一阶段**的架构演进方向。R1-R7 解决了"跑得稳"的问题，本轮关注"跑得更聪明"。

> 注意每个子功能优化完成后都要在本文档记录下，以免下个协作者做重复了

---

## 核心判断

R1-R7 完成后，OmniCore 的执行内核已具备基本工程治理（上下文截断、MessageBus、graph 拆分、工具标签、Plan 持久化、Prompt 分离、Session Memory）。对照 Claude Code 的生产级实现，差距从"缺失"转向"深度"：

| 维度 | OmniCore 现状（R1-R7 后） | Claude Code 做法 | 差距 |
|------|--------------------------|-----------------|------|
| Prompt 管理 | 静态/动态 `.txt` 文件拆分，字符串拼接 | Section 数组，每段独立缓存/token 计数/toggle | 可观测性、cache hit 率 |
| 上下文成本 | 截断 + snip，压缩后无状态恢复 | Token 预算制 + 五级压缩 + 压缩后重注入 plan/artifact/tool 声明 | 长任务"失忆" |
| 会话持久化 | mutable snapshot（pickle/JSON） | Append-only JSONL event log，写简读复 | 审计、断点续传、并发安全 |
| 工具执行 | 能力标签 + 批次调度 | 六阶段 pipeline（schema→语义校验→依赖注入→权限→执行→结果规范化） | 健壮性、可扩展性 |
| 多 Agent | 单层 agent_registry 调度 | 三级模式（subagent → coordinator → swarm） | 复杂任务分解能力 |
| 安全模型 | policy_engine 审批，工具有 destructive 标签 | Fail-closed default + 分层信任（内置 > MCP > IDE）+ 权限桥接 | 深度防御 |

---

## 演进方向总览

| ID | 方向 | 优先级 | 预估工作量 | 详细方案 | 状态 |
|----|------|--------|-----------|---------|------|
| S1 | Prompt Section Registry（section 化 + token 预算 + cache 标记） | **P0** | 2-3 天 | [S1 方案](2026-04-02-S1-Prompt-Section-Registry方案.md) | ✅ 已完成（2026-04-02） |
| S2 | 上下文预算制 + 压缩重注入 | **P0** | 2 天 | [S2 方案](2026-04-02-S2-上下文预算制与压缩重注入方案.md) | 🔲 未开始 |
| S3 | Session Event Sourcing（append-only event log） | **P1** | 3-4 天 | [S3 方案](2026-04-02-S3-Session-Event-Sourcing方案.md) | 🔲 未开始 |
| S4 | Tool 六阶段执行 Pipeline | **P1** | 2-3 天 | [S4 方案](2026-04-02-S4-Tool执行Pipeline方案.md) | 🔲 未开始 |
| S5 | 多 Agent 协作（Coordinator + Subagent） | **P2** | 4-5 天 | [S5 方案](2026-04-02-S5-多Agent协作方案.md) | 🔲 未开始 |
| S6 | Fail-Closed 安全分层 | **P2** | 2 天 | [S6 方案](2026-04-02-S6-Fail-Closed安全分层方案.md) | 🔲 未开始 |

---

## S1: Prompt Section Registry

**优先级**：P0 — 直接降 API 成本（prompt cache hit），提升可观测性

**问题**：R6 把 router prompt 拆成了 static/dynamic 两个 `.txt` 文件，但本质仍是字符串拼接。无法按 section 做 token 预算、无法 toggle 单个 section、无法追踪哪段 prompt 占了多少 token。其他节点（critic、replanner、browser）的 prompt 仍是单文件全量。

**Claude Code 做法**：`getSystemPrompt()` 返回 `string[]`，每个 section 有 `name`、`cacheable` 标记、token 计数。有 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 分隔静态/动态。要打破缓存必须显式标记 `DANGEROUS_uncachedSection()`。

**详细方案**：[S1 方案](2026-04-02-S1-Prompt-Section-Registry方案.md)

**完成记录**（2026-04-02）：
- 新建 `core/prompt_registry.py`（PromptSection + PromptRegistry + `build_single_section_prompt`）
- 新建 `tests/test_prompt_registry_unit.py`（25 个测试）
- 修改 `config/settings.py`（+3 配置项：`PROMPT_SECTION_CACHE_ENABLED`/`PROMPT_TOKEN_BUDGET`/`DEBUG_PROMPT`）
- 修改 `core/router.py`（新增 `_build_prompt_registry()`，system prompt 通过 registry 组装）
- 修改 `agents/critic.py`、`core/replanner.py`、`core/finalizer.py`、`core/session_memory.py`（统一走 registry）
- 全量测试通过

---

## S2: 上下文预算制 + 压缩重注入

**优先级**：P0 — 长任务不再"失忆"

**问题**：R1 实现了 tool result 截断和 history snip，但缺少全局 token 预算分配。压缩后丢失当前 plan 状态、artifact 引用、工具声明，agent 需要重新"热身"。R7 的 session memory 缓解了部分问题，但注入时机和内容粒度不够精细。

**Claude Code 做法**：
- 预留 20k token 给 auto-compact 操作本身
- 各部分有明确预算：system prompt、memory、tool results、history
- 压缩后重注入：当前打开的文件 + 活跃 Plan + 工具声明 + session memory
- 压缩失败有 circuit breaker（连续 3 次失败停止）

**详细方案**：[S2 方案](2026-04-02-S2-上下文预算制与压缩重注入方案.md)

---

## S3: Session Event Sourcing

**优先级**：P1 — 审计能力、断点续传可靠性大幅提升

**问题**：当前 session/job 状态以 mutable snapshot 持久化（`runtime_state_store.py`），每次状态变更覆写整个文件。问题：无法审计历史变更、并发写不安全、断点续传依赖快照完整性。

**Claude Code 做法**：
- Append-only JSONL event log（`{sessionId}.jsonl`），写路径极简
- 所有复杂性放在读/恢复路径：conversation graph 重建、断链修复、orphan tool result 恢复
- Metadata 定期 re-append 到文件尾部（fast list loading 优化）
- Subagent sidechain 允许 UUID 重复（继承父上下文）

**详细方案**：[S3 方案](2026-04-02-S3-Session-Event-Sourcing方案.md)

---

## S4: Tool 六阶段执行 Pipeline

**优先级**：P1 — 工具执行健壮性和可扩展性

**问题**：R4 给工具加了能力标签，但执行流程仍然是"调用 → 拿结果"两步。缺少 schema 校验、语义校验、依赖注入、权限检查等前置阶段，也缺少结果规范化后处理。新工具接入需要在多个地方写适配逻辑。

**Claude Code 做法**：六阶段 pipeline：
1. Zod schema 校验（编译时类型安全）
2. 语义 `validateInput()`（如检查黑名单路径）
3. `backfillObservableInput()`（隐式依赖注入，如路径展开）
4. `runPreToolUseHooks()`（权限判定：allow/deny/ask）
5. `tool.call()`（实际执行）
6. `tool_result` 生成（错误规范化，返回模型可理解的格式）

**详细方案**：[S4 方案](2026-04-02-S4-Tool执行Pipeline方案.md)

---

## S5: 多 Agent 协作（Coordinator + Subagent）

**优先级**：P2 — 复杂任务分解能力

**问题**：当前 `agent_registry.py` 支持单层 agent 调度（planner → executor），但不支持：一个 agent 派发多个子 agent 并行执行再汇总（coordinator 模式），也不支持 agent 间消息传递（mailbox 模式）。

**Claude Code 做法**：三级模式逐级升级：
1. **Subagent**：继承父上下文，完成后返回结果。`AgentTool` 统一入口。
2. **Coordinator**：主线程 system prompt 替换为编排者，派发 research/implementation/verification worker，结果以 `<task-notification>` XML 返回。
3. **Swarm/Teammates**：持久化团队文件、共享任务列表、独立信箱、权限桥接。Teammate 不能 spawn teammate（防止无限递归）。

**详细方案**：[S5 方案](2026-04-02-S5-多Agent协作方案.md)

---

## S6: Fail-Closed 安全分层

**优先级**：P2 — 深度防御

**问题**：R4 的 `destructive` 标签和 `policy_engine.py` 的审批机制提供了基本安全保障，但缺少分层信任模型。内置工具和 MCP 工具享有同等信任级别，MCP 工具缺少额外约束（如 description 长度上限、shell 执行限制）。

**Claude Code 做法**：
- 所有新 tool 默认 fail-closed（非并发、非只读、destructive）
- 分层信任：内置 > 本地 > MCP remote，MCP skill 禁止执行嵌入式 shell
- MCP tool description 上限 2048 字符（防止上下文膨胀）
- IDE 工具有白名单（`ALLOWED_IDE_TOOLS`）
- 权限桥接：子 agent 权限请求路由到主 agent 统一审批

**详细方案**：[S6 方案](2026-04-02-S6-Fail-Closed安全分层方案.md)

---

## 依赖关系

```
S1 (Prompt Section Registry)  ───→ S2 (上下文预算制，
                                      需 section 级 token 计数基础)

S2 (上下文预算制)              独立（在 R1 基础上增强）

S3 (Session Event Sourcing)    独立（替换现有持久化层）

S4 (Tool 执行 Pipeline)        独立（在 R4 基础上增强）
                               ───→ S6 (安全分层，
                                      复用 pipeline 的权限阶段)

S5 (多 Agent 协作)             依赖 S4（子 agent 工具执行走统一 pipeline）
                               依赖 R2（agent 间通信走 MessageBus）

S6 (Fail-Closed 安全分层)      依赖 S4（权限检查嵌入执行 pipeline）
```

**推荐执行顺序**：S1 → S2 → S4 → S3（可与 S4 并行）→ S6 → S5

---

## 与 R 系列的关系

本文档是 R1-R7 的**延续**，不是替代。

| R 系列基础 | S 系列增强 | 关系 |
|-----------|-----------|------|
| R1（截断 + snip） | S2（预算制 + 重注入） | S2 在 R1 之上加全局预算分配和压缩后状态恢复 |
| R4（工具标签） | S4（六阶段 pipeline） | S4 将 R4 的标签嵌入完整执行流水线 |
| R6（static/dynamic 拆分） | S1（section registry） | S1 将 R6 的两文件拆分升级为多 section 注册表 |
| R7（session memory） | S2（压缩重注入） | S2 让 R7 的 session memory 在压缩后自动重注入 |
| R2（MessageBus） | S5（多 Agent） | S5 利用 R2 的 MessageBus 做 agent 间通信 |

---

## 配置项汇总（规划中）

| 环境变量 | 默认值 | 所属 | 说明 |
|----------|--------|------|------|
| `PROMPT_SECTION_CACHE_ENABLED` | `true` | S1 | Section 级 prompt 缓存开关 |
| `PROMPT_TOKEN_BUDGET` | `4000` | S1 | System prompt 总 token 预算 |
| `CONTEXT_RESERVE_TOKENS` | `20000` | S2 | 为 auto-compact 预留的 token 数 |
| `CONTEXT_COMPACT_THRESHOLD` | `0.85` | S2 | 触发 compact 的上下文使用率阈值 |
| `COMPACT_MAX_CONSECUTIVE_FAILURES` | `3` | S2 | Compact 连续失败熔断次数 |
| `SESSION_EVENT_LOG_ENABLED` | `false` | S3 | Event sourcing 开关 |
| `SESSION_EVENT_FLUSH_INTERVAL` | `5` | S3 | Event 批量 flush 间隔（秒） |
| `TOOL_PIPELINE_STRICT_MODE` | `false` | S4 | 严格模式：校验失败直接拒绝而非降级 |
| `MCP_DESCRIPTION_MAX_LENGTH` | `2048` | S6 | MCP 工具描述最大字符数 |
| `MCP_TRUST_LEVEL` | `restricted` | S6 | MCP 工具默认信任等级 |

---

## 给下一个智能体的执行指引

1. **先看总览表**：文档顶部的演进方向总览表有每项的最新状态
2. **按依赖顺序执行**：S1 → S2 → S4 → S3 → S6 → S5
3. **每完成一步，更新对应行的状态**：`🔲 未开始` → `🔨 进行中` → `✅ 已完成`
4. **每完成一个 S 项，在对应子文档的步骤表下方补充**：
   - 新建文件列表
   - 修改文件列表
   - 实现说明（与原方案的偏差、关键设计决策）
   - 验收结果
5. **同时更新本主文档**对应 section 的完成记录
6. **遇到与现有代码冲突时**：以当前代码为准，更新方案描述
7. **全量回归**：每个 S 项完成后跑 `pytest tests -q`，确保不破坏现有功能
8. **参考 R 系列的完成记录格式**：见 `2026-04-01-Runtime架构优化规划.md` 中 R1-R7 的完成记录
