# S4: Tool 六阶段执行 Pipeline 方案

> 所属规划：[Claude Code 启发架构演进规划](2026-04-02-Claude-Code启发架构演进规划.md)
>
> 优先级：P1 | 预估工作量：2-3 天 | 状态：🔲 未开始
>
> 前置依赖：R4（已完成，工具能力标签化）

---

## 问题分析

R4 给工具加了 `concurrent_safe`、`output_type`、`needs_network`、`destructive` 四个能力标签，调度器据此决定并行/串行。但工具执行本身仍是简单的"调用 → 拿结果"两步：

1. **无输入校验**：工具参数直接传入执行，格式错误只能靠工具自己处理
2. **无语义校验**：如 file_worker 收到系统关键路径、browser_agent 收到恶意 URL，缺少前置检查
3. **无依赖注入**：工具需要的环境信息（当前目录、session context）靠调用者手动拼装
4. **权限检查分散**：`policy_engine` 检查在 graph 层，不在 tool 执行层，新增工具容易遗漏
5. **结果格式不统一**：各 worker 返回格式不同（有的 dict、有的 string），下游需要多处适配
6. **新工具接入成本高**：需要在 `tool_registry`、`tool_adapters`、`task_executor` 多处添加适配逻辑

## Claude Code 参考实现

```
// Tool interface 强制声明：
//   isConcurrencySafe(), isReadOnly(), isDestructive(),
//   checkPermissions(), validateInput(), renderToolUseMessage(),
//   interruptBehavior()
//
// 六阶段 pipeline:
//   1. Zod schema validation
//   2. semantic validateInput()
//   3. backfillObservableInput() (依赖注入)
//   4. runPreToolUseHooks() (权限: allow/deny/ask)
//   5. tool.call() (实际执行)
//   6. tool_result generation (错误规范化)
//
// schema 校验失败不 crash，返回错误让模型自我修正
// StreamingToolExecutor 支持流式并发执行
```

## 详细实现步骤

### S4-1: ToolPipeline 框架

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | 新建 `core/tool_pipeline.py`，定义 `ToolPipelineStage` 枚举：`SCHEMA_VALIDATE`、`SEMANTIC_VALIDATE`、`INJECT_CONTEXT`、`CHECK_PERMISSION`、`EXECUTE`、`NORMALIZE_RESULT` | `core/tool_pipeline.py`（新建） | 🔲 |
| 2 | 定义 `ToolExecutionContext` dataclass：`tool_name`、`tool_spec`、`raw_params`、`validated_params`、`injected_params`、`permission_result`、`raw_result`、`normalized_result`、`stage_errors: list` | 同上 | 🔲 |
| 3 | 实现 `ToolPipeline` 类：`execute(tool_name, params, state) -> ToolExecutionContext`，按顺序执行六阶段 | 同上 | 🔲 |
| 4 | 每阶段失败可配置行为：`strict_mode=True` 时直接拒绝，`False` 时降级到下一阶段并记 warning | 同上 | 🔲 |

### S4-2: Stage 1 — Schema 校验

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | `ToolSpec` 新增 `param_schema: Optional[dict]`（JSON Schema 格式） | `core/tool_protocol.py` | 🔲 |
| 2 | 内置工具补充 param_schema 声明 | `core/tool_registry.py` | 🔲 |
| 3 | MCP 工具自动从 MCP Server 元数据获取 inputSchema | `core/mcp_client.py` | 🔲 |
| 4 | Pipeline 中用 jsonschema 做校验，失败时返回结构化错误信息（字段名 + 期望类型 + 实际值） | `core/tool_pipeline.py` | 🔲 |

### S4-3: Stage 2 — 语义校验

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | `ToolSpec` 新增 `validate_input: Optional[Callable]`，每个工具可注册自定义校验函数 | `core/tool_protocol.py` | 🔲 |
| 2 | 内置校验规则：file_worker 拒绝系统关键路径（`/etc/`, `/usr/`）、browser_agent URL 域名白名单（可选） | `core/tool_registry.py` | 🔲 |
| 3 | MCP 工具默认无语义校验（可通过 `mcp_servers.yaml` 配置 `blocked_params`） | `config/mcp_servers.yaml` | 🔲 |

### S4-4: Stage 3 — 上下文注入

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | 实现 `inject_context(params, state) -> params`：自动补充 `cwd`、`session_id`、`job_id`、`timeout` | `core/tool_pipeline.py` | 🔲 |
| 2 | 工具可声明 `required_context: list[str]`（如 `["cwd", "session_id"]`），pipeline 自动从 state 注入 | `core/tool_protocol.py` | 🔲 |
| 3 | 路径参数自动展开：`~` → home dir、相对路径 → 绝对路径 | `core/tool_pipeline.py` | 🔲 |

### S4-5: Stage 4 — 权限检查

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | 将 `policy_engine.evaluate_risk()` 调用从 graph 层移入 pipeline | `core/tool_pipeline.py` | 🔲 |
| 2 | 权限结果三态：`allow`（直接执行）、`deny`（拒绝 + 返回原因）、`ask`（触发 human-in-the-loop） | 同上 | 🔲 |
| 3 | `destructive=True` 且无 pre-approved 时默认走 `ask` | 同上 | 🔲 |
| 4 | 保留 graph 层的 `human_confirm_node` 作为 `ask` 的 UI 端实现 | `core/graph_nodes.py`（不改动） | 🔲 |

### S4-6: Stage 6 — 结果规范化

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | 定义 `ToolResult` dataclass：`success: bool`、`output: str`、`structured_data: Optional[dict]`、`artifacts: list`、`error: Optional[str]`、`error_type: Optional[str]` | `core/tool_pipeline.py` | 🔲 |
| 2 | 各 worker 返回统一转换为 `ToolResult`（在 pipeline 末端，不改 worker 内部实现） | 同上 | 🔲 |
| 3 | 错误规范化：Python exception → `ToolResult(success=False, error=str(e), error_type=type(e).__name__)` | 同上 | 🔲 |
| 4 | `task_executor.py` 的 `_apply_task_outcome()` 改为消费 `ToolResult` 而非 raw dict | `core/task_executor.py` | 🔲 |

### S4-7: 集成与迁移

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | `task_executor.py` 的 `_dispatch_single_task()` 改为调用 `ToolPipeline.execute()` | `core/task_executor.py` | 🔲 |
| 2 | 渐进迁移：`TOOL_PIPELINE_STRICT_MODE=false` 时，校验失败降级执行（与旧行为兼容） | 同上 | 🔲 |
| 3 | Pipeline 执行日志：每个阶段的耗时和结果记入 debug log | `core/tool_pipeline.py` | 🔲 |

### S4-8: 测试

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | Pipeline 全阶段单元测试（正常流程、各阶段失败、strict vs 降级模式） | `tests/test_tool_pipeline_unit.py`（新建） | 🔲 |
| 2 | Schema 校验测试（正确参数、缺失字段、类型错误） | 同上 | 🔲 |
| 3 | 语义校验测试（危险路径拒绝、正常路径通过） | 同上 | 🔲 |
| 4 | 结果规范化测试（各 worker 返回格式 → ToolResult） | 同上 | 🔲 |
| 5 | 全量回归 | `pytest tests -q` | 🔲 |

---

## 核心设计决策

1. **不改 worker 内部**：Pipeline 包裹在 worker 外层，worker 的 `execute()` / `execute_async()` 签名不变。规范化在 pipeline 末端做
2. **渐进式启用**：`TOOL_PIPELINE_STRICT_MODE=false` 时退化为旧行为 + warning log，降低迁移风险
3. **不引入 Pydantic/Zod**：用标准 `jsonschema` 库做 schema 校验，避免新增重依赖。MCP 工具的 inputSchema 天然就是 JSON Schema 格式
4. **权限检查位置变更**：从 graph 层下沉到 pipeline 层，更内聚。graph 层的 `human_confirm_node` 保留为 UI 交互入口
5. **ToolResult 不替代现有 outcome dict**：ToolResult 是 pipeline 内部的结构化中间产物，通过 `to_outcome_dict()` 转换后传入现有 `_apply_task_outcome()` 流程

## 验收标准

- 所有工具执行经过六阶段 pipeline
- Schema 校验失败返回结构化错误信息，模型可据此自我修正
- 危险路径 / 系统路径被语义校验拦截
- 所有 worker 返回统一转换为 `ToolResult`
- 新工具只需注册 `ToolSpec`（含 param_schema + capabilities），无需修改 task_executor
- 全量测试通过
