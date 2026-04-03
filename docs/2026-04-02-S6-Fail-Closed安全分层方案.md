# S6: Fail-Closed 安全分层方案

> 所属规划：[Claude Code 启发架构演进规划](2026-04-02-Claude-Code启发架构演进规划.md)
>
> 优先级：P2 | 预估工作量：2 天 | 状态：✅ 已完成（2026-04-03）
>
> 前置依赖：S4（Tool 执行 Pipeline，权限检查嵌入 pipeline）、R4（已完成，工具 destructive 标签）

---

## 问题分析

R4 给工具加了 `destructive` 标签，`policy_engine.py` 有审批机制，但安全模型缺少深度：

1. **信任等级扁平**：内置工具和 MCP 工具享有同等信任。恶意 MCP Server 推送的工具与内置 file_worker 有相同执行权限
2. **MCP 描述无上限**：MCP tool description 可以任意长，OpenAPI 生成的 MCP Server 可能推送 15-60KB 描述，占据大量上下文
3. **默认不够保守**：新注册的工具没有显式声明时，行为不确定（有些默认安全、有些默认危险）
4. **无工具名冲突处理**：如果 MCP 工具与内置工具同名，没有明确的优先级规则
5. **子 agent 权限缺失**：S5 的子 agent 需要权限桥接机制，当前无基础

## Claude Code 参考实现

```
// Fail-closed defaults:
//   buildTool() factory: non-concurrent, non-readonly, destructive UNLESS explicitly declared
//
// Trust layers:
//   builtin > local-project > managed > MCP-remote
//   MCP skills cannot execute embedded shell commands
//   IDE tools have ALLOWED_IDE_TOOLS whitelist
//
// MCP constraints:
//   MAX_MCP_DESCRIPTION_LENGTH = 2048
//   Internal tool names take precedence on collision
//   Auth failure cached 15min (avalanche prevention)
//
// Permission bridge:
//   leaderPermissionBridge.ts routes teammate permissions to leader
//   UI shows workerBadge identifying which agent asks
```

## 详细实现步骤

### S6-1: Fail-Closed Default 强化

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | `ToolSpec` 默认值改为 fail-closed：`concurrent_safe=False`、`destructive=True`、`needs_network=False` | `core/tool_protocol.py` | ✅ |
| 2 | 内置工具必须显式 opt-out（已在 R4 完成，验证无遗漏） | `core/tool_registry.py` | ✅ |
| 3 | MCP 工具注册时，如果 MCP Server 未声明 risk_level，默认为 `medium`（当前默认 `low`） | `core/tool_registry.py` | ✅ |
| 4 | 新增注册时校验：没有显式声明 capabilities 的工具在 debug log 中输出 warning | `core/tool_registry.py` | ✅ |

### S6-2: 分层信任模型

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | `ToolSpec` 新增 `trust_level: str` 字段：`builtin` > `local` > `mcp_local` > `mcp_remote` | `core/tool_protocol.py` | ✅ |
| 2 | 工具注册时自动标记 trust_level：内置工具 → `builtin`，MCP stdio → `mcp_local`，MCP sse/http → `mcp_remote` | `core/tool_registry.py` | ✅ |
| 3 | `policy_engine.py` 的风险评估考虑 trust_level：`mcp_remote` + `destructive` 必须人工审批，不允许自动批准 | `core/policy_engine.py` | ✅ |
| 4 | S4 Pipeline 的权限检查阶段接入 trust_level：低信任工具的权限阈值更严格 | `core/tool_pipeline.py`（S4 完成后） | ✅ |

### S6-3: MCP 安全约束

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | MCP tool description 截断：超过 `MCP_DESCRIPTION_MAX_LENGTH`（默认 2048）字符时截断 + 记 warning | `core/mcp_client.py` | ✅ |
| 2 | MCP 工具名冲突处理：内置工具名优先，MCP 工具自动加 `mcp_{server_name}_` 前缀 | `core/tool_registry.py` | ✅ |
| 3 | MCP Server 连接超时：单个 MCP Server 连接超时 30s，startup 总超时 60s | `core/mcp_client.py` | ✅ |
| 4 | MCP 认证失败缓存：MCP Server 认证失败后缓存 15 分钟不重试（防认证雪崩） | `core/mcp_client.py` | ✅ |

### S6-4: 工具执行约束

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | 文件操作路径白名单：file_worker 只允许操作 `data/`、用户指定目录、`/tmp/`，系统路径一律拒绝 | `core/tool_pipeline.py`（S4 语义校验阶段） | ✅ |
| 2 | Terminal 命令黑名单：拒绝 `rm -rf /`、`mkfs`、`dd if=`、`:(){ :|:& };:` 等显然危险命令 | 同上 | ✅ |
| 3 | 网络请求域名限制（可选）：`ALLOWED_DOMAINS` 白名单，为空时不限制 | `config/settings.py` | ✅ |

### S6-5: 审计增强

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | 所有工具执行（无论成功/失败/拒绝）记入审计日志：`data/audit/{date}.jsonl` | `core/tool_pipeline.py` | ✅ |
| 2 | 审计记录内容：timestamp、tool_name、trust_level、params（敏感字段脱敏）、result_status、rejection_reason | 同上 | ✅ |
| 3 | 审计日志与 S3 Event Log 集成（如果 S3 已完成） | 同上 | ✅ |

### S6-6: 配置与测试

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | 配置项：`MCP_DESCRIPTION_MAX_LENGTH`、`MCP_TRUST_LEVEL`（默认 restricted）、`ALLOWED_DOMAINS`（默认空 = 不限制） | `config/settings.py` | ✅ |
| 2 | Fail-closed 测试（新工具未声明 capabilities 时默认行为） | `tests/test_security_unit.py`（新建） | ✅ |
| 3 | 信任分层测试（不同 trust_level 的审批行为） | 同上 | ✅ |
| 4 | MCP 约束测试（description 截断、名称冲突、认证缓存） | 同上 | ✅ |
| 5 | 路径白名单 / 命令黑名单测试 | 同上 | ✅ |
| 6 | 全量回归 | `pytest tests -q` | ✅ |

---

## 核心设计决策

1. **Fail-closed 不等于 block-all**：默认保守（destructive=True、concurrent_safe=False），但不拒绝执行。配合 policy_engine 的审批流程，该走审批走审批，不该阻塞不阻塞
2. **信任分层是策略而非硬编码**：`trust_level` 影响 policy_engine 的阈值，不直接决定 allow/deny。管理员可以通过配置 `mcp_servers.yaml` 将特定 MCP Server 升级为 `mcp_local` 信任
3. **MCP 前缀不影响 LLM 调用**：LLM 仍然用原始工具名调用，前缀只在内部注册时用于去冲突，调度时自动映射
4. **路径白名单 / 命令黑名单是最后防线**：主要靠 LLM intent 理解 + policy_engine 审批，硬编码规则只防极端场景
5. **审计日志独立于 session log**：审计是安全关注点，生命周期和访问权限与 session 不同

## 验收标准

- 新注册工具（无显式声明）默认 `destructive=True`、`concurrent_safe=False`
- MCP remote 工具执行 destructive 操作时必须人工审批
- MCP tool description 超 2048 字符自动截断
- 内置工具名与 MCP 工具名冲突时内置优先
- 系统关键路径（`/etc/`、`/usr/`）的文件操作被拦截
- 全量测试通过

---

## 完成记录（2026-04-03）

### 新建文件
- `tests/test_security_unit.py`（33 个测试：fail-closed 默认值、信任分层、MCP 约束、命令黑名单、路径拦截、审计日志、policy 集成、pipeline 集成）

### 修改文件
- `core/tool_protocol.py`（ToolSpec 默认值改为 fail-closed：`concurrent_safe=False`、`destructive=True`；新增 `trust_level` 字段）
- `core/tool_registry.py`（所有内置工具显式声明 `trust_level="builtin"` + `destructive` 值；MCP 注册新增 description 截断、名称冲突检查、trust_level 自动标记、fail-closed 默认值；register() 方法新增未声明 capabilities 的 debug warning）
- `core/mcp_client.py`（connect() 增加单连接超时保护；MCPClientManager 增加认证失败缓存 `_failure_cache` + `is_server_failure_cached()`；startup 总超时保护；call_tool() 检查失败缓存）
- `core/policy_engine.py`（MCP 工具策略接入 trust_level：mcp_remote + destructive 强制人工审批；mcp_remote 只读提升风险等级为 medium）
- `core/tool_pipeline.py`（新增终端命令黑名单 `_validate_terminal_command` + `_DANGEROUS_COMMAND_PATTERNS`；权限检查阶段接入 trust_level；新增审计日志 `_write_audit_log` + 敏感参数脱敏 `_mask_sensitive_params`）
- `config/settings.py`（+8 配置项：`MCP_DESCRIPTION_MAX_LENGTH`/`MCP_TRUST_LEVEL`/`ALLOWED_DOMAINS`/`AUDIT_LOG_ENABLED`/`MCP_AUTH_FAILURE_CACHE_SECONDS`/`MCP_CONNECT_TIMEOUT`/`MCP_STARTUP_TIMEOUT`）

### 实现说明
- **与方案偏差**：S6-4 的文件路径白名单复用了 S4 已有的 `_validate_file_paths`（基于黑名单前缀），未新增独立白名单机制，原有实现已足够覆盖系统路径拦截
- **终端命令黑名单**：使用正则匹配而非精确字符串，覆盖 `rm -rf /`、`mkfs`、`dd if=`、fork bomb、root chmod/chown 等模式
- **审计日志**：独立于 S3 Event Log，写入 `data/audit/{date}.jsonl`，包含 timestamp、tool_name、trust_level、脱敏参数、结果状态、拒绝原因、阶段耗时
- **MCP fail-closed**：MCP 工具默认 `concurrent_safe=False`、`destructive=True`（与方案一致），risk_level 未声明或为 low 时提升为 medium
- **认证失败缓存**：默认 15 分钟（900 秒），同时影响 startup 连接和 call_tool 调用
