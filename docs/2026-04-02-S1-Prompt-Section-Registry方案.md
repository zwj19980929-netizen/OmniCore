# S1: Prompt Section Registry 方案

> 所属规划：[Claude Code 启发架构演进规划](2026-04-02-Claude-Code启发架构演进规划.md)
>
> 优先级：P0 | 预估工作量：2-3 天 | 状态：✅ 已完成
>
> 前置依赖：R6（已完成，static/dynamic prompt 拆分）

---

## 问题分析

R6 将 router prompt 拆成了 `router_system_static.txt` + `router_system_dynamic.txt`，但：

1. **仍是字符串拼接**：`_build_system_prompt()` 返回一个大字符串，无法按 section 做 token 计数
2. **无 token 预算**：不知道 system prompt 总共占了多少 token，也无法给单个 section 设上限
3. **不可观测**：debug 时看不到每段 prompt 分别占了多少 token、是否命中 cache
4. **其他节点未覆盖**：critic、replanner、browser、finalize 的 prompt 仍是单文件全量加载
5. **无 toggle 能力**：不能按场景关闭不需要的 section（如简单查询不需要 plan 指引）

## Claude Code 参考实现

```
// getSystemPrompt() 返回 string[]
// 每个 section 通过 systemPromptSection() 或 DANGEROUS_uncachedSection() 创建
// 有 SYSTEM_PROMPT_DYNAMIC_BOUNDARY 分隔符
// 每个 section 有 name、cacheBreak、content
```

关键设计：
- Section 是一等公民，有 name 和 cacheable 标记
- 静态 section 在前（走 API prompt cache），动态 section 在后
- 要打破缓存必须显式声明为 "DANGEROUS"（命名约定防误用）
- Section 级 token 计数支持预算控制

## 详细实现步骤

### S1-1: PromptSection 数据模型

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | 新建 `core/prompt_registry.py`，定义 `PromptSection` dataclass：`name: str`、`content: str`、`cacheable: bool`（默认 True）、`enabled: bool`（默认 True）、`max_tokens: Optional[int]` | `core/prompt_registry.py`（新建） | ✅ |
| 2 | 实现 `PromptRegistry` 类：`register(section)`、`get_sections(filter)`、`render() -> str`、`token_report() -> dict` | 同上 | ✅ |
| 3 | `render()` 逻辑：按注册顺序拼接 enabled sections，静态在前 + `--- DYNAMIC BOUNDARY ---` + 动态在后 | 同上 | ✅ |
| 4 | `token_report()` 返回每个 section 的 name、token_count、cacheable 标记（用 tiktoken 或字符估算） | 同上 | ✅ |

### S1-2: Router Prompt 迁移

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | 将 `router_system_static.txt` 注册为 `router_static` section（cacheable，priority=100） | `core/router.py` | ✅ |
| 2 | 将动态 Agent 能力注册为 `agent_capabilities` section（non-cacheable，priority=80） | `core/router.py` | ✅ |
| 3 | 将动态工具目录注册为 `tool_catalog` section（non-cacheable，priority=70） | `core/router.py` | ✅ |
| 4 | `_build_system_prompt()` 改为通过 `PromptRegistry.render()` 组装 | `core/router.py` | ✅ |

### S1-3: 其他节点 Prompt 迁移

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | Critic prompt（`critic_system.txt`）通过 `build_single_section_prompt()` 注册 | `agents/critic.py` | ✅ |
| 2 | Replanner prompt（`replanner_system_en.txt`）通过 `build_single_section_prompt()` 注册 | `core/replanner.py` | ✅ |
| 3 | Finalize prompt（`finalize_system_static.txt`）通过 `build_single_section_prompt()` 注册 | `core/finalizer.py` | ✅ |
| 4 | Session memory extract prompt 通过 `build_single_section_prompt()` 注册 | `core/session_memory.py` | ✅ |

### S1-4: Token 预算 + 可观测性

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | `PromptRegistry.render()` 增加超预算截断：单 section 超 `max_tokens` 时截断并标记 `[…truncated]` | `core/prompt_registry.py` | ✅ |
| 2 | 总预算限制：所有 section token 总和超 `total_budget` 时，从最低优先级 section 开始关闭 | 同上 | ✅ |
| 3 | `token_report()` + `log_report()` 输出详细报告，支持 `DEBUG_PROMPT=true` 时自动输出 | 同上 | ✅ |
| 4 | 配置项 `PROMPT_SECTION_CACHE_ENABLED`、`PROMPT_TOKEN_BUDGET`、`DEBUG_PROMPT` | `config/settings.py` | ✅ |

### S1-5: 测试

| 步骤 | 任务 | 涉及文件 | 状态 |
|------|------|---------|------|
| 1 | PromptSection / PromptRegistry 单元测试（注册、渲染、token 计数、预算截断、toggle） | `tests/test_prompt_registry_unit.py`（新建） | ✅ |
| 2 | Router prompt 迁移回归测试（确保输出格式不变） | `tests/test_router_unit.py` | ✅ |
| 3 | 全量回归 | `pytest tests -q` | ✅ |

---

## 核心设计决策

1. **Section 粒度**：按语义拆分（identity/rules/format），不按文件拆分。一个 `.txt` 文件可以包含多个 section（用 `---` 或 `## ` 分隔），也可以一个 section 对应一个文件
2. **Token 计数方式**：优先用 tiktoken（精确），fallback 到字符数 / 4 估算（避免 tiktoken 依赖问题）
3. **向后兼容**：`render()` 返回的字符串与原始拼接结果语义等价，迁移期间可 A/B 对比
4. **不做 prompt cache API 集成**：S1 只做本地 section 管理，API 层的 prompt cache（如 Anthropic `cache_control`）由 `core/llm.py` 根据 `cacheable` 标记自动处理，留到 S1 完成后单独集成

## 验收标准

- 所有 LLM 调用的 system prompt 通过 `PromptRegistry` 组装
- `token_report()` 能输出每个 section 的 name 和 token 占比
- 单 section 超预算时自动截断，总预算超限时低优先级 section 自动关闭
- 全量测试通过

---

## 实现记录

> 完成时间：2026-04-02

### 新建文件
- `core/prompt_registry.py` — PromptSection dataclass + PromptRegistry 类 + `build_single_section_prompt()` 便利函数
- `tests/test_prompt_registry_unit.py` — 25 个单元测试

### 修改文件
- `config/settings.py` — 新增 `PROMPT_SECTION_CACHE_ENABLED`、`PROMPT_TOKEN_BUDGET`、`DEBUG_PROMPT` 配置项
- `core/router.py` — 新增 `_build_prompt_registry()` 方法，`_build_system_prompt()` 改为通过 registry 组装，`_build_dynamic_context()` 返回空字符串（动态内容已合入 system prompt）
- `agents/critic.py` — 通过 `build_single_section_prompt()` 构建 system prompt
- `core/replanner.py` — 同上
- `core/finalizer.py` — 同上
- `core/session_memory.py` — 同上

### 与原方案的偏差
1. **Router section 粒度调整**：原方案计划将 static prompt 拆为 `identity`/`rules`/`output_format` 三个 section 对应三个文件。实际实现保持 `router_static` 为单个 section（对应 `router_system_static.txt`），避免拆分现有 prompt 文件引入不必要的风险。未来可按需进一步细分。
2. **动态上下文合入 system prompt**：原 R6 设计将动态上下文注入 user_message 前缀（为了 LLM API cache hit）。S1 将其合入 system prompt 的 dynamic section（通过 `cacheable=False` 标记），由 `render()` 统一组装。`_build_dynamic_context()` 保留但返回空字符串以保持兼容。
3. **简单节点用便利函数**：对于只有单个 prompt 的节点（critic/replanner/finalizer/session_memory），新增 `build_single_section_prompt()` 便利函数，避免每个节点都手动创建 registry 实例。

### 验收结果
- `pytest tests/test_prompt_registry_unit.py -q` — 25 passed
- `pytest tests -q` — 全量回归通过
