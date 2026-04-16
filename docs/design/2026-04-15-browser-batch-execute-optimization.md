# Browser 批量执行与按需纠偏优化方案

> Status: draft
> Created: 2026-04-15
> Last updated: 2026-04-15

## 1. 问题分析

### 1.1 当前执行模型

当前 BrowserAgent 的主循环是 **"每步决策"模式**：每执行一个浏览器动作（点击、输入、选择等），都要经过完整的 LLM 决策链路。

以 P2 关闭（默认）时的单步 LLM 调用链为例：

```
_execute_step (每步循环)
  ├─ _plan_next_action
  │   ├─ _try_unified()          → 1 次 LLM（unified_plan）
  │   ├─ _try_assess()           → 1 次 LLM（page_assessment）
  │   └─ _decide_action_with_llm → 1 次 LLM（action_decision，前两个都 WAIT 时触发）
  ├─ _execute_action             → 0 次 LLM（纯浏览器操作）
  └─ step_advance                → 1 次 LLM（判断 plan step 是否完成）
```

即使开启 P2（`BROWSER_UNIFIED_ACT_ENABLED=true`），仍然是 **每步 1 次 LLM（browser_act）+ 1 次 step_advance = 2 次/步**。

### 1.2 实际代价

以登录任务为例（导航 → 输入用户名 → 输入密码 → 点击登录 → 验证结果）：

| 模式 | LLM 调用次数 | 使用 deepseek-reasoner 的耗时 |
|---|---|---|
| P2 关闭（当前默认） | 3~4 × 5 步 = **15~20 次** | 15~30 分钟 |
| P2 开启 | 2 × 5 步 = **10 次** | 10~15 分钟 |
| **本方案** | **2~3 次（规划 + 验证 + 可能的纠偏）** | **2~4 分钟** |

### 1.3 核心问题

1. **不区分规划与执行**：每步都重新做完整决策，而非"规划一次 → 执行多步"
2. **不区分模型档次**：生成结构化动作序列和深度推理分析都用同一个推理模型
3. **依赖 LLM 做可用 DOM 信号就能判断的事**：step_advance 用 LLM 判断"输入是否成功"，而 DOM value 变化就够了

## 2. 设计目标

- **一次规划，批量执行**：感知页面后一次 LLM 调用生成完整动作序列，批量执行不再逐步调模型
- **按需纠偏，分层响应**：DOM 检查点做轻量校验，异常时视觉模型定位问题，纠偏才调推理模型
- **分层模型选择**：结构化动作序列用快模型，纠偏/重规划用推理模型
- **向后兼容**：通过开关控制，可随时回退到逐步决策模式

## 3. 整体架构

```
┌───────────────────────────────────────────────────────────────┐
│                    BrowserAgent.run()                          │
│                                                               │
│  ┌─────────────┐    ┌──────────────────┐    ┌──────────────┐ │
│  │ 1. 感知页面  │───▶│ 2. 生成动作序列   │───▶│ 3. 批量执行  │ │
│  │ (a11y+DOM)  │    │ (快模型,1次调用)  │    │ (0次LLM)    │ │
│  └─────────────┘    └──────────────────┘    └──────┬───────┘ │
│                                                     │         │
│                                              DOM 检查点       │
│                                              (零成本)         │
│                                                     │         │
│                                            ┌────────▼───────┐ │
│                                            │ 4. 结果验证     │ │
│                                            │ (视觉模型截图)  │ │
│                                            └────────┬───────┘ │
│                                                     │         │
│                                     ┌───────────────┼────┐    │
│                                     │               │    │    │
│                                  成功 ✓         小偏差   大偏差 │
│                                     │               │    │    │
│                                   结束        局部纠偏  重规划 │
│                                            (快模型)  (推理模型)│
│                                               │         │     │
│                                               └────┬────┘     │
│                                                    │          │
│                                              回到步骤 1       │
└───────────────────────────────────────────────────────────────┘
```

## 4. 详细设计

### 4.1 动作序列规划（ActionSequence）

#### 4.1.1 数据结构

新增 `agents/browser_action_sequence.py`：

```python
@dataclass
class SequenceAction:
    """动作序列中的单个动作，带具体元素绑定"""
    action_type: str          # click, input, select, press_key, scroll, wait, navigate
    target_ref: str           # 元素引用 (如 el_1)
    target_selector: str      # CSS 选择器（备选定位）
    value: str                # 输入值 / 滚动距离 / URL 等
    description: str          # 动作描述
    dom_checkpoint: dict      # 预期的 DOM 变化信号（见 4.3）

@dataclass
class ActionSequence:
    """一次 LLM 调用生成的完整动作序列"""
    actions: List[SequenceAction]
    goal_description: str     # 本轮序列的目标描述
    expected_outcome: str     # 预期的最终结果（用于视觉验证）
    execution_index: int = 0  # 当前执行到第几个

    def current_action(self) -> Optional[SequenceAction]
    def advance(self) -> Optional[SequenceAction]
    def remaining(self) -> List[SequenceAction]
    def is_complete(self) -> bool
```

#### 4.1.2 Prompt 设计

新增 `prompts/browser_action_sequence.txt`：

与当前 `browser_act.txt` 的关键区别：
- 输入相同（页面感知数据、任务、元素列表）
- 输出从**单个 action** 变成 **action 数组**，每个 action 带 `dom_checkpoint`
- 包含 `expected_outcome` 字段，描述执行完所有动作后的预期状态

```
返回 schema:
{
  "thinking": "分析页面状态和任务需求",
  "goal_description": "本轮操作目标",
  "expected_outcome": "执行完毕后的预期页面状态描述",
  "actions": [
    {
      "type": "input",
      "target_ref": "el_1",
      "target_selector": "#username",
      "value": "admin",
      "description": "输入用户名",
      "dom_checkpoint": {
        "type": "value_change",
        "target_ref": "el_1",
        "expected_value": "admin"
      }
    },
    ...
  ]
}
```

#### 4.1.3 模型选择

动作序列生成是结构化输出任务，不需要深度推理。通过 `ModelCapability` 扩展支持分层选择：

- 新增能力类型 `BROWSER_ACTION_SEQUENCE`（或复用 `TEXT_CHAT`）
- 新增环境变量 `BROWSER_SEQUENCE_MODEL` 控制动作序列生成使用的模型
- 默认使用快模型（如 `gpt-4o-mini` / `deepseek-chat`），不走推理模型
- 纠偏/重规划时才升级到 `DEFAULT_MODEL`（推理模型）

### 4.2 批量执行引擎

在 `BrowserAgent` 中新增 `_batch_execute` 方法，替代当前的逐步 `_execute_step` 循环：

```python
async def _batch_execute(
    self, sequence: ActionSequence, task_intent: TaskIntent
) -> BatchResult:
    """
    批量执行动作序列。
    每个动作执行后仅做 DOM 检查点校验，不调 LLM。
    """
    for action in sequence.remaining():
        # 1. 执行动作（复用已有的 _execute_action）
        success = await self._execute_action(action)

        # 2. DOM 检查点校验（零 LLM 成本）
        if not success:
            return BatchResult(status="action_failed", failed_at=action)

        checkpoint_ok = await self._verify_dom_checkpoint(action.dom_checkpoint)
        if not checkpoint_ok:
            return BatchResult(status="checkpoint_failed", failed_at=action)

        sequence.advance()

    return BatchResult(status="completed")
```

关键原则：
- **不引入新的执行逻辑**，复用已有的 `_execute_action`、`_recover_action` 等
- 每个动作之间**不调 LLM**，只做 DOM 检查点
- 如果动作执行失败或检查点不通过，立即中断，交给纠偏流程

### 4.3 DOM 检查点（零成本校验）

新增 `utils/dom_checkpoint.py`：

DOM 检查点是 LLM 在生成动作序列时一起输出的，描述每个动作执行后的预期 DOM 变化。验证时不需要 LLM，只需简单的 DOM 查询。

支持的检查点类型：

| 类型 | 含义 | 验证方式 |
|---|---|---|
| `value_change` | 输入框的值变了 | 读取元素 `.value` 属性 |
| `url_change` | URL 发生跳转 | 比较 `page.url` |
| `element_appear` | 某元素出现 | `querySelector` |
| `element_disappear` | 某元素消失 | `querySelector` 返回 null |
| `text_appear` | 页面出现特定文本 | `page.textContent` 包含 |
| `attribute_change` | 元素属性变化 | 读取属性值 |
| `none` | 无需检查（如 scroll） | 直接通过 |

```python
async def verify_dom_checkpoint(
    page, checkpoint: dict
) -> CheckpointResult:
    """
    纯 DOM 操作，零 LLM 成本。
    返回 (passed: bool, detail: str)
    """
```

当 LLM 未输出 `dom_checkpoint` 或输出了 `"none"` 时，默认通过——不阻塞执行。

### 4.4 结果验证与纠偏

#### 4.4.1 验证策略

批量执行完成后（或中途因检查点失败中断后），进入验证阶段：

```python
async def _verify_and_correct(
    self, sequence: ActionSequence, task: str, batch_result: BatchResult
) -> VerifyResult:
```

**验证分层：**

1. **DOM 级验证**（零成本）：URL 是否符合预期、页面是否有错误提示（toast/alert）、关键元素是否存在
2. **视觉验证**（1 次 vision 模型调用）：截图 + `expected_outcome` 描述 → 视觉模型判断是否达成目标

视觉模型返回：
```json
{
  "goal_achieved": true/false,
  "deviation": "none" | "minor" | "major",
  "detail": "描述当前页面实际状态与预期的差异"
}
```

#### 4.4.2 纠偏策略

根据验证结果分层处理：

| 偏差级别 | 处理方式 | 模型选择 |
|---|---|---|
| `none` | 任务完成 | 无 |
| `minor`（小偏差） | 重新感知页面 → 快模型生成补充动作序列 → 批量执行 | 快模型（`BROWSER_SEQUENCE_MODEL`） |
| `major`（大偏差） | 重新感知页面 → 推理模型分析失败原因并重新规划 → 新的动作序列 → 批量执行 | 推理模型（`DEFAULT_MODEL`） |

纠偏次数由 `BROWSER_MAX_CORRECTIONS` 控制（默认 `BROWSER_MAX_REPLANS` 的值）。

#### 4.4.3 纠偏上下文

纠偏时 LLM 收到的信息包括：
- 原始任务描述
- 原始动作序列（哪些执行了，哪些没执行）
- 失败点的 DOM 状态 / 视觉截图描述
- 当前页面的最新感知数据

这让 LLM 能判断"是整体方向错了，还是某个操作细节需要调整"。

### 4.5 与现有架构的集成

#### 4.5.1 执行流程切换

在 `BrowserAgent.run()` 的主循环中，新增批量执行分支：

```python
if settings.BROWSER_BATCH_EXECUTE_ENABLED:
    # 新路径：一次规划 → 批量执行 → 验证纠偏
    result = await self._run_batch_mode(task, task_intent, ...)
else:
    # 旧路径：逐步决策（当前逻辑，完全保留）
    for step_no in range(1, max_steps + 1):
        step_result = await self._execute_step(...)
```

#### 4.5.2 与 P1 TaskPlan 的关系

- TaskPlan（高层规划）仍然保留，作为多页面任务的宏观指引
- ActionSequence（动作序列）是 TaskPlan 某一个 step 的具体执行细节
- 批量执行模式下，`step_advance` 不再逐步调 LLM，而是由视觉验证结果直接推进 TaskPlan

```
TaskPlan (高层)
  Step 0: "打开登录页并填写凭据"
    → ActionSequence: [navigate, input username, input password, click submit]
    → 批量执行 → 视觉验证 → step_advance(无 LLM)
  Step 1: "验证登录是否成功"
    → ActionSequence: [extract page title/content]
    → 批量执行 → 视觉验证 → step_advance(无 LLM)
```

#### 4.5.3 与 P0 指纹去重的关系

指纹去重在批量执行模式下依然有效：
- 动作序列生成时，prompt 中注入 `{repeated_actions}` 黑名单
- 批量执行过程中，每个动作仍调用 `_record_action` 记录指纹

#### 4.5.4 与 P2 单 Prompt 的关系

- `BROWSER_BATCH_EXECUTE_ENABLED` 优先级高于 `BROWSER_UNIFIED_ACT_ENABLED`
- 批量模式开启时，P2 的 `browser_act.txt` 仅在纠偏阶段作为单步决策的 fallback
- 两者不冲突，可以同时开启

## 5. 配置项

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `BROWSER_BATCH_EXECUTE_ENABLED` | `false` | 批量执行模式总开关 |
| `BROWSER_SEQUENCE_MODEL` | (空，使用快模型自动选择) | 动作序列生成使用的模型 |
| `BROWSER_MAX_SEQUENCE_ACTIONS` | `10` | 单次动作序列最大动作数 |
| `BROWSER_MAX_CORRECTIONS` | `2` | 最大纠偏次数 |
| `BROWSER_DOM_CHECKPOINT_ENABLED` | `true` | 是否启用 DOM 检查点 |
| `BROWSER_VISUAL_VERIFY_ENABLED` | `true` | 批量执行后是否视觉验证 |
| `BROWSER_CORRECTION_ESCALATE_TO_REASONING` | `true` | major 偏差是否升级到推理模型 |

所有配置项在 `config/settings.py` 中定义，支持环境变量覆盖。

## 6. 新增 / 修改文件

| 文件 | 变更类型 | 说明 |
|---|---|---|
| `agents/browser_action_sequence.py` | **新增** | ActionSequence 数据结构 + LLM 规划 + 纠偏逻辑 |
| `utils/dom_checkpoint.py` | **新增** | DOM 检查点验证 |
| `prompts/browser_action_sequence.txt` | **新增** | 动作序列生成 prompt |
| `prompts/browser_visual_verify.txt` | **新增** | 视觉验证 prompt |
| `prompts/browser_correction.txt` | **新增** | 纠偏规划 prompt |
| `agents/browser_agent.py` | 修改 | `run()` 新增批量执行分支，新增 `_run_batch_mode` |
| `agents/browser_decision.py` | 修改 | 新增 `generate_action_sequence` 方法 |
| `config/settings.py` | 修改 | 新增配置项 |
| `core/model_registry.py` | 修改 | 新增 `BROWSER_SEQUENCE_MODEL` 路由支持 |
| `tests/test_browser_batch_execute_unit.py` | **新增** | 单元测试 |

## 7. 实施计划

### Phase 1：核心数据结构 + 批量执行引擎
- `ActionSequence` 数据结构
- `dom_checkpoint.py` DOM 检查点验证
- `_batch_execute` 方法
- 单元测试

### Phase 2：动作序列 Prompt + 模型分层
- `browser_action_sequence.txt` prompt
- `BROWSER_SEQUENCE_MODEL` 配置与模型路由
- `generate_action_sequence` 集成到 `BrowserDecisionLayer`

### Phase 3：视觉验证 + 纠偏
- `browser_visual_verify.txt` prompt
- `browser_correction.txt` prompt
- `_verify_and_correct` 方法
- 纠偏分层逻辑（minor/major）

### Phase 4：集成 + 开关
- `run()` 中新增批量执行分支
- 与 TaskPlan / 指纹去重 / P2 的集成
- `config/settings.py` 配置项
- 集成测试

## 8. 风险与缓解

| 风险 | 缓解措施 |
|---|---|
| 动态页面：动作序列中后续元素在前面操作后可能变化（如弹窗、动态加载） | DOM 检查点失败时中断批量执行，进入纠偏；纠偏时重新感知页面 |
| 快模型生成的动作序列质量不够 | `BROWSER_SEQUENCE_MODEL` 可配置，也可直接用推理模型；纠偏机制兜底 |
| 视觉验证误判 | 视觉验证结果仅作为纠偏触发条件，不影响已执行的操作；`BROWSER_VISUAL_VERIFY_ENABLED` 可关闭 |
| 与现有逐步决策模式的兼容性 | `BROWSER_BATCH_EXECUTE_ENABLED` 默认关闭，旧路径完全保留 |
