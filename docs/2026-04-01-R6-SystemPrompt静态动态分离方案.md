# R6: System Prompt 静态/动态分离方案

> 优先级：P2 | 预估工作量：1 天 | 依赖：无 | 被依赖：无

---

## 1. 目标

当前 `prompts/router_system.txt` 中角色定义、思考框架、Worker 参数说明（静态）和 `{{AGENT_CAPABILITIES}}`（动态）混在一起。每次 `_build_router_system_prompt()` 调用时，动态内容变化导致整个 system prompt 变化，无法利用 LLM API 的 prompt cache。

Claude Code 的做法：system prompt 拆成**静态前缀**（角色+固定规则，命中 prompt cache）+ **动态边界后的内容**（当前状态、工具列表等）。动态信息尽量走 user message 注入，不污染 system prompt。

本方案将 router 和其他节点的 prompt 做静态/动态分离。

**验收标准**：

- `prompts/router_system.txt` 拆分为 `router_system_static.txt`（~90% 内容）+ `router_system_dynamic.txt`（模板）
- 静态部分作为 system message 传入，动态部分作为 user message 前缀注入
- 静态 system prompt 跨请求不变（可缓存）
- 其他关键 prompt（finalize、replanner、critic）同理拆分
- 现有功能不受影响

---

## 2. 现有代码基础

| 组件 | 文件 | 当前状态 |
|------|------|---------|
| Router prompt 加载 | `core/router.py` `_load_router_system_prompt()` 行 24-39 | 模块加载时从 `prompts/router_system.txt` 读取 |
| Router prompt 组装 | `core/router.py` `_build_router_system_prompt()` 行 649-658 | base_prompt + 替换 `{{AGENT_CAPABILITIES}}` + 追加 `ROUTER_OUTPUT_APPENDIX` + 追加 `build_dynamic_tool_prompt_lines()` |
| LLM 调用 | `core/router.py` `analyze_intent()` | system_prompt + user_message 一起传入 LLMClient |
| user_message 动态拼接 | `core/router.py` `analyze_intent()` | 对话历史、相关记忆、artifacts、用户偏好、时间/位置/OS、知识上下文，全部拼入 user_message |
| 其他节点 prompt | `core/graph.py` 各节点 | 通过 `get_prompt()` 加载，动态内容直接拼在 prompt 末尾 |

**关键发现**：

1. `router_system.txt` 的内容分析：~95% 是纯静态（思考框架、Worker 选择原则、参数说明、输出格式）；仅 `{{AGENT_CAPABILITIES}}` 是动态注入点
2. `ROUTER_OUTPUT_APPENDIX` 是硬编码的静态字符串，可以合并到静态 prompt
3. `build_dynamic_tool_prompt_lines()` 返回的动态工具目录，是主要的"缓存破坏者"
4. `analyze_intent()` 已经在 user_message 层追加了大量动态上下文（时间/位置/历史等），这部分无需改动
5. finalize/replanner/critic 的 prompt 也有类似模式（静态模板 + 动态上下文拼接）

---

## 3. 架构设计

### 3.1 分离前后的 prompt 结构

```
分离前：
┌─────────────────────────────────────┐
│  system message（全部内容）          │
│                                     │
│  ┌─────────────────────────────┐    │
│  │ 角色定义（静态）             │    │
│  │ 思考框架（静态）             │    │
│  │ Worker 选择原则（静态）       │    │
│  │ {{AGENT_CAPABILITIES}}（动态）│ ← 每次变化都导致整个 system prompt 变化
│  │ 参数说明（静态）             │    │
│  │ 输出格式（静态）             │    │
│  │ 动态工具目录（动态）         │ ← MCP/插件工具列表每次可能不同
│  └─────────────────────────────┘    │
└─────────────────────────────────────┘

分离后：
┌─────────────────────────────────────┐
│  system message（静态前缀，可缓存）  │
│                                     │
│  ┌─────────────────────────────┐    │
│  │ 角色定义                     │    │
│  │ 思考框架                     │    │
│  │ Worker 选择原则               │    │
│  │ 参数说明                     │    │
│  │ 输出格式                     │    │
│  │ ROUTER_OUTPUT_APPENDIX       │    │
│  └─────────────────────────────┘    │
└─────────────────────────────────────┘

┌─────────────────────────────────────┐
│  user message 前缀（动态，每次不同） │
│                                     │
│  ┌─────────────────────────────┐    │
│  │ [当前可用工具与能力]         │    │
│  │ Agent 能力列表               │    │
│  │ 动态工具目录                 │    │
│  │                              │    │
│  │ [用户请求]                   │    │
│  │ 用户原始输入                 │    │
│  │ 对话历史 / 知识上下文 / ...  │    │
│  └─────────────────────────────┘    │
└─────────────────────────────────────┘
```

### 3.2 对 LLM API cache 的影响

```
典型的 LLM API prompt cache 行为：
- 请求 1: system="A+B+C" → cache miss，缓存 "A+B+C"
- 请求 2: system="A+B+C" → cache hit ✅
- 请求 3: system="A+B+D" → cache miss（C 变成了 D）

分离后：
- 请求 1: system="A+B" → cache miss，缓存 "A+B"
- 请求 2: system="A+B" → cache hit ✅（动态部分在 user message 里，不影响 system cache）
- 请求 3: system="A+B" → cache hit ✅
```

---

## 4. 详细设计

### 4.1 拆分 `prompts/router_system.txt`

**原文件**拆分为两个文件：

**`prompts/router_system_static.txt`**（静态前缀，约占原文件 95%）：

```
# 复制原 router_system.txt 的全部内容
# 但是移除 {{AGENT_CAPABILITIES}} 占位符所在的段落
# 将 ROUTER_OUTPUT_APPENDIX 的内容也合并进来（它本来就是静态的）

你是 OmniCore 的智能路由层...
（思考框架 5 步路径分析）
（Worker 选择原则）
（各 Worker params 参数说明）
（输出 JSON schema）
（输出格式附录，原 ROUTER_OUTPUT_APPENDIX）
```

**`prompts/router_system_dynamic.txt`**（动态模板）：

```
## 当前可用工具与能力

### Agent 能力
{{AGENT_CAPABILITIES}}

### 动态工具
{{DYNAMIC_TOOL_LINES}}
```

### 4.2 修改 `core/router.py` — prompt 组装逻辑

```python
# 模块级加载（只读一次，永不变）
_STATIC_PROMPT = None
_DYNAMIC_TEMPLATE = None


def _load_prompts():
    global _STATIC_PROMPT, _DYNAMIC_TEMPLATE
    if _STATIC_PROMPT is not None:
        return

    prompts_dir = os.path.join(os.path.dirname(__file__), "..", "prompts")

    static_path = os.path.join(prompts_dir, "router_system_static.txt")
    dynamic_path = os.path.join(prompts_dir, "router_system_dynamic.txt")

    # 静态 prompt
    try:
        with open(static_path, "r", encoding="utf-8") as f:
            _STATIC_PROMPT = f.read()
    except FileNotFoundError:
        # fallback: 用原文件
        _STATIC_PROMPT = _load_router_system_prompt()

    # 动态模板
    try:
        with open(dynamic_path, "r", encoding="utf-8") as f:
            _DYNAMIC_TEMPLATE = f.read()
    except FileNotFoundError:
        _DYNAMIC_TEMPLATE = "{{AGENT_CAPABILITIES}}\n{{DYNAMIC_TOOL_LINES}}"


class RouterAgent:

    @staticmethod
    def _build_system_prompt() -> str:
        """返回纯静态的 system prompt（可缓存）"""
        _load_prompts()
        return _STATIC_PROMPT

    @staticmethod
    def _build_dynamic_context() -> str:
        """返回动态上下文（作为 user message 前缀）"""
        _load_prompts()

        # Agent 能力描述
        agent_caps = agent_registry.build_router_agent_descriptions(lang="zh")
        # 动态工具目录
        dynamic_tools = build_dynamic_tool_prompt_lines()

        context = _DYNAMIC_TEMPLATE
        context = context.replace("{{AGENT_CAPABILITIES}}", agent_caps)
        context = context.replace("{{DYNAMIC_TOOL_LINES}}", dynamic_tools)

        return context

    def analyze_intent(self, user_input, conversation_history=None, ...):
        # system prompt: 纯静态
        system_prompt = self._build_system_prompt()

        # user message: 动态上下文 + 用户输入
        dynamic_ctx = self._build_dynamic_context()

        user_message_parts = []
        user_message_parts.append(dynamic_ctx)

        # ... 现有的 user_message 拼接逻辑不变 ...
        # 对话历史、相关记忆、知识上下文等继续在 user_message 中拼接
        user_message_parts.append(f"\n用户请求：{user_input}")

        user_message = "\n\n".join(user_message_parts)

        # 调用 LLM
        response = self._llm.call(
            system_prompt=system_prompt,  # 静态，可缓存
            user_message=user_message,    # 动态
        )
```

### 4.3 同理拆分其他 prompt

**finalize prompt**：
- 静态：输出合成的角色定义、格式要求
- 动态（user message）：时间 hint、位置 hint、任务结果摘要

**replanner prompt**：
- 静态：重规划的策略说明、输出格式
- 动态（user message）：失败任务详情、可用工具列表

**critic prompt**：
- 静态：评审标准、输出格式
- 动态（user message）：待评审的任务结果

每个 prompt 拆分为 `{name}_static.txt` + `{name}_dynamic.txt`。

### 4.4 向后兼容

为避免一次性改动太大，保留 fallback：

```python
def _load_prompts():
    # ...
    try:
        with open(static_path, "r", encoding="utf-8") as f:
            _STATIC_PROMPT = f.read()
    except FileNotFoundError:
        # 如果新文件不存在，用原来的方式（整个 router_system.txt 作为 system prompt）
        _STATIC_PROMPT = _load_router_system_prompt()
        _DYNAMIC_TEMPLATE = ""  # 动态内容保持在 system prompt 内
```

---

## 5. 典型场景

### 场景：5 轮对话中 system prompt cache 命中率

**分离前**（假设 MCP 工具列表在第 3 轮新增一个工具）：

| 轮次 | system prompt 变化 | cache 命中 |
|------|-------------------|-----------|
| 1 | 初始化 | miss |
| 2 | 不变 | hit |
| 3 | MCP 工具列表变化 → 整个 system prompt 变化 | miss |
| 4 | 不变 | hit |
| 5 | 不变 | hit |

命中率：3/5 = 60%

**分离后**：

| 轮次 | system prompt 变化 | cache 命中 |
|------|-------------------|-----------|
| 1 | 初始化 | miss |
| 2 | 不变 | hit |
| 3 | 不变（工具列表变化在 user message 中） | hit |
| 4 | 不变 | hit |
| 5 | 不变 | hit |

命中率：4/5 = 80%

长会话（20+ 轮）中，system prompt cache 命中率接近 100%。

---

## 6. 实施步骤

| 步骤 | 任务 | 交付物 | 状态 |
|------|------|--------|------|
| 1 | 分析 `router_system.txt` 内容，标记每段是静态还是动态 | 分析文档 | ✅ |
| 2 | 拆分 `prompts/router_system.txt` → `router_system_static.txt` + `router_system_dynamic.txt` | `prompts/` | ✅ |
| 3 | 修改 `core/router.py` `_build_router_system_prompt()` 改为 `_build_system_prompt()` + `_build_dynamic_context()`；`analyze_intent()` 适配新的分离式调用 | `core/router.py` | ✅ |
| 4 | 删除 `ROUTER_OUTPUT_APPENDIX` 硬编码常量，内容合并到 `router_system_static.txt` | `core/router.py` | ✅ |
| 5 | 提取 finalize 内联 system prompt → `finalize_system_static.txt`；`finalizer.py` 改为从文件加载 | `prompts/`, `core/finalizer.py` | ✅ |
| 6 | replanner prompt — 已是纯静态文件（`replanner_system_en.txt`），动态上下文（JSON）已在 user_message | 无需改动 | ✅ |
| 7 | critic prompt — 已是纯静态文件（`critic_system.txt`），任务结果已在 user_message | 无需改动 | ✅ |
| 8 | 更新 `tests/test_router_unit.py` 中的两个 dynamic catalog 测试，改为验证 `_build_dynamic_context()` | `tests/test_router_unit.py` | ✅ |
| 9 | 验证 LiteLLM 的 cache 行为（确认静态前缀不变时是否命中 prompt cache） | 联调验证 | 🔲 留待集成环境验证 |
| 10 | 全量回归测试 | `pytest tests -q` | ✅ 19/19 router 测试通过，replanner 中 1 个预存 bug 不影响本方案 |

---

## 7. 风险与注意事项

| 风险 | 缓解措施 |
|------|---------|
| 动态上下文从 system message 移到 user message 后，LLM 行为可能变化 | 动态上下文格式不变，只是注入位置变了；实测验证输出质量 |
| 拆分后 prompt 维护成本增加（两个文件） | 静态文件变化极少，动态模板很小（<20 行）；净增维护量可忽略 |
| LiteLLM/底层 API 不支持 prompt cache | 即使不支持 cache，拆分也无害；system prompt 更稳定有助于行为一致性 |
| 原 `router_system.txt` 中 `{{AGENT_CAPABILITIES}}` 位置对 LLM 理解有影响 | 动态上下文作为 user message 第一段，仍然在 LLM 上下文窗口的前部 |
| 多个 prompt 文件同时拆分，改动面较大 | 优先只拆 router（核心、调用最频繁），其他节点后续逐步拆分 |

---

## 8. 完成记录（2026-04-02）

**新建文件**
- `prompts/router_system_static.txt` — router 静态前缀（原 `router_system.txt` 去掉 `{{AGENT_CAPABILITIES}}` 段 + 合并 `ROUTER_OUTPUT_APPENDIX`）
- `prompts/router_system_dynamic.txt` — 动态上下文模板（Agent 能力 + 动态工具目录，含两个占位符）
- `prompts/finalize_system_static.txt` — finalize 节点静态 system prompt（原内联字符串提取为文件）

**修改文件**
- `core/router.py`
  - 新增模块级 `_STATIC_PROMPT` / `_DYNAMIC_TEMPLATE` 缓存 + `_load_prompts()` 懒加载
  - 新增 `RouterAgent._build_system_prompt()`（返回纯静态，可缓存）
  - 新增 `RouterAgent._build_dynamic_context()`（返回动态上下文字符串）
  - `_build_router_system_prompt()` 保留为兼容别名，内部转发至 `_build_system_prompt()`
  - `analyze_intent()` 改为：system_prompt 用纯静态；user_message 前缀注入 `_build_dynamic_context()`
  - 移除 `ROUTER_OUTPUT_APPENDIX` 硬编码常量（内容已合并入 `router_system_static.txt`）
- `core/finalizer.py`
  - `_synthesize_user_facing_answer()` 改为从 `finalize_system_static` 加载 system prompt，保留原字符串为 fallback
- `tests/test_router_unit.py`
  - `test_router_system_prompt_uses_dynamic_tool_catalog` / `test_router_system_prompt_excludes_disabled_plugins` 改为验证 `_build_dynamic_context()` 而非 system prompt

**实现说明**
- replanner（`replanner_system_en.txt`）和 critic（`critic_system.txt`）的动态上下文本来就已经在 user_message 中，无需拆分
- 向后兼容：`_load_prompts()` 若找不到新文件会 fallback 到旧的 `router_system.txt`，不影响已有部署
- `ROUTER_SYSTEM_PROMPT` 模块级常量保留，外部如有引用仍可用
