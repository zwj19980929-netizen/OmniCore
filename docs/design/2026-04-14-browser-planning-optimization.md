# Browser 自我规划优化方案

> Status: in-progress (P0 + P2 + P1 数据层落地 2026-04-14；P3 未实施)
> Created: 2026-04-14
> Last updated: 2026-04-14

## 背景与问题

用户反馈：浏览器 Agent 在执行 web 任务时"自我规划很蠢，反复提出一样的 plan，像没有记忆"。

定位到以下根因（代码位置引用）：

1. **短期记忆过短**
   - `agents/browser_decision.py:126` 仅保存 `_action_history`（最近 6 个 action 签名）。
   - Prompt 里仅注入最近 3 步（`agents/browser_decision.py:1920` `recent_steps[-3:]`），LLM 看不到更长的历史。

2. **无任务级 plan 对象**
   - 现有 `_plan_next_action`（`agents/browser_decision.py:2480`）每一步都把整题重新丢给 LLM 做"下一步干啥"的反应式决策。
   - 没有"先搜 A → 抽 B → 比较 C"这种显式 step list，LLM 每次都从零规划，自然反复得到相似的结果。

3. **无环境指纹去重**
   - 同一个 `(url, page_stage, 候选元素集合)` 再出现时，没有任何机制阻止重复产出同一个 action。
   - `_recent_failed_action_matches` 只拦"最近失败"的，不拦"重复的成功尝试"。

4. **三份 Prompt 并行执行，互相覆盖**
   - `_plan_next_action` 并行跑 `_unified_plan_action` + `_assess_page_with_llm`，失败再补 `_decide_action_with_llm` 与 `_reflect_on_failures`。
   - 单步决策 2–4 次 LLM 调用，prompt 规则大量重复，抖动明显且昂贵。
   - 三个 prompt 文件：`prompts/browser_unified_plan.txt`、`prompts/browser_page_assessment.txt`、`prompts/browser_action_decision.txt`，decision rules 几乎相同。

5. **Reflection 触发条件单一**
   - `_should_reflect`（`agents/browser_decision.py:2418`）只在"连续失败 N 次"时触发。
   - 死循环（成功但无进展）、重复规划不会触发反思。

## 改进目标

| 指标 | 当前（估计） | 目标 |
|---|---|---|
| 重复 action 率（同指纹 ≥ 2 次 / 总步数） | ~20% | < 5% |
| 平均任务步数 | baseline | 下降 ≥ 30% |
| 单任务 LLM 调用数 | baseline | 下降 ≥ 40% |
| 单步 LLM 调用数 | 2–4 次 | 1–2 次 |

---

## 分期方案

### P0 — 短期记忆 + 指纹去重（1–2 天）

**动机：** 先用最小代价消除"反复同 plan"的直观症状，不改架构。

**改动：**

1. `agents/browser_decision.py`
   - 新增 `self._step_fingerprints: OrderedDict[str, int] = OrderedDict()`，记录最近 N 步的 `(url_path, page_stage, action_type, target_ref or selector, normalized_value)` 指纹 → 命中次数。
   - 新增方法 `_fingerprint_action(action, url, page_stage) -> str`、`_is_repeat_action(fp) -> bool`。
   - 在 `_sanitize_planned_action` 末尾增加：若 action 指纹命中 ≥ `BROWSER_DEDUP_THRESHOLD`（默认 2），则置为 `FAILED` 触发 fallback；同时写入 `web_debug_recorder` 便于定位。
   - `recent_steps` 注入长度从 3 扩到 `BROWSER_RECENT_STEPS_IN_PROMPT`（默认 8）。

2. Prompt 改动（`prompts/browser_unified_plan.txt`、`prompts/browser_page_assessment.txt`、`prompts/browser_action_decision.txt`）
   - 新增变量 `{repeated_actions}`：列出已重复 ≥ 2 次的 action 签名。
   - 规则增补一条：`- Actions listed under "Repeated actions" are BLACKLISTED. Do NOT propose any of them again; choose a different target, a different action type, or DONE/EXTRACT.`

3. `config/settings.py`
   - `BROWSER_STEP_MEMORY_SIZE = int(os.getenv("BROWSER_STEP_MEMORY_SIZE", "20"))`
   - `BROWSER_DEDUP_THRESHOLD = int(os.getenv("BROWSER_DEDUP_THRESHOLD", "2"))`
   - `BROWSER_RECENT_STEPS_IN_PROMPT = int(os.getenv("BROWSER_RECENT_STEPS_IN_PROMPT", "8"))`

4. 测试：新建 `tests/test_browser_step_dedup_unit.py`
   - 同指纹 action 第 2 次被拒绝。
   - 不同 `page_stage` 下同 action 不算重复。
   - `recent_steps` 长度截断正确。

**验收：** 在本地 10 个历史失败任务上回放，重复 action 率下降至 < 5%。

---

### P1 — 任务级 Plan 对象（3–4 天）

**动机：** 根治"每步都从零规划"。让决策改成"推进 plan 的第 k 步"，而不是"下一步干啥"。

**改动：**

1. 新增 `agents/browser_task_plan.py`
   - 数据类：
     ```python
     @dataclass
     class PlanStep:
         index: int
         goal: str                 # e.g. "在 Google 搜 'meta llama'"
         success_criteria: str     # e.g. "URL 包含 google.com/search 且页面有搜索结果卡"
         hint: str                 # e.g. "用核心实体名即可"
         status: str = "pending"   # pending | active | done | skipped
     
     @dataclass
     class TaskPlan:
         task: str
         steps: List[PlanStep]
         current_index: int = 0
         revisions: int = 0
     ```
   - `async def build_initial_plan(task, intent, llm) -> TaskPlan`：任务开始时调用一次 LLM。
   - `async def step_advance(plan, observation, llm) -> AdvanceDecision`：判定当前 step 是否完成 / 跳过 / 需要 replan。
   - `async def replan(plan, failure_reason, llm) -> TaskPlan`：复用 `core/replanner.py` 的模式，改写剩余 steps。

2. `agents/browser_agent.py` / `browser_decision.py`
   - `BrowserAgent.run(task)` 入口处构造 `TaskPlan` 并保存到 `self.decision._task_plan`。
   - `_plan_next_action` prompt 注入：
     - `{plan_current_step}`、`{plan_completed_steps}`、`{plan_remaining_steps}`
   - 每步 action 执行完成后调用 `step_advance`：
     - `done` → `current_index += 1`
     - `stuck` 且重试次数超阈 → `replan()`；`revisions` 超阈（默认 2）则任务失败。

3. 新 Prompt（短小）
   - `prompts/browser_task_plan.txt`：输入 task + intent，输出 `steps: [{goal, success_criteria, hint}]`。
   - `prompts/browser_step_advance.txt`：输入 current_step + observation，输出 `{advance: bool, reason, need_replan: bool}`。

4. `config/settings.py`
   - `BROWSER_MAX_PLAN_STEPS = int(os.getenv("BROWSER_MAX_PLAN_STEPS", "8"))`
   - `BROWSER_MAX_REPLANS = int(os.getenv("BROWSER_MAX_REPLANS", "2"))`
   - `BROWSER_STEP_STUCK_THRESHOLD = int(os.getenv("BROWSER_STEP_STUCK_THRESHOLD", "4"))`

5. 测试：`tests/test_browser_task_plan_unit.py`
   - 初始 plan 生成格式正确。
   - `step_advance` 正确识别完成 / 未完成 / 需要 replan。
   - `replan` 保留已完成 steps，改写剩余。

**验收：** 在同一批回放任务上平均步数下降 ≥ 30%。

---

### P2 — 三合一 Prompt 收敛（2 天）

**动机：** 消除单步 2–4 次 LLM 的浪费和互相覆盖带来的抖动。

**改动：**

1. 合并 `browser_unified_plan.txt` 与 `browser_page_assessment.txt` 为新的 `prompts/browser_act.txt`
   - 返回 schema 统一为：
     ```json
     {
       "thinking": "...",
       "goal_satisfied": false,
       "action": {...},   // 与现有 action schema 一致
       "confidence": 0.8,
       "need_replan": false
     }
     ```
   - 删除重复的 decision rules，用单一组精简规则。

2. `agents/browser_decision.py::_plan_next_action` 重构
   - 去掉 `asyncio.gather(_try_unified(), _try_assess())`。
   - 单次调用 `_act_with_llm(...)` → `_sanitize_planned_action` → 失败才走 `_choose_observation_driven_action` / `_find_search_result_click_action` / `_decide_action_locally` 规则 fallback。
   - 移除 `browser_action_decision.txt`（后续版本删除文件）。

3. 失败-重试策略
   - 当 sanitize 拒绝或 LLM 返回 `WAIT` 时，允许**一次**重试并把 `repeated_actions` 和拒绝原因再写入 prompt；再不行才走规则 fallback。

**验收：** 单步 LLM 调用数从 2–4 降到 1–2；单任务总 token 下降 ≥ 40%。

---

### P3 — 跨会话长期记忆（可选，1 周）

**动机：** 对相似任务复用历史成功 plan，避免每次从零规划。

**改动：**

1. 新增 `utils/browser_plan_memory.py`
   - `record_success(task_signature, domain, plan, metrics)`：任务成功时写入。
   - `recall(task_signature, domain, top_k=3) -> List[TaskPlan]`：基于 embedding 相似度检索。
   - 底层复用现有 `memory/` Chroma 向量库（`CHROMA_PERSIST_DIR`）。

2. `agents/browser_task_plan.py::build_initial_plan`
   - 先检索 top-k 相似历史 plan，作为 `plan_seed` 注入 `browser_task_plan.txt` 的 prompt。
   - LLM 可选择采纳 / 改写 / 放弃 seed。

3. Hook 到 `core/session_memory.py`，不新增独立持久化层。

4. 开关：`BROWSER_PLAN_MEMORY_ENABLED`（默认 `false`，P3 稳定后再默认开启）。

---

## 配置项汇总（新增）

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `BROWSER_STEP_MEMORY_SIZE` | 20 | 指纹窗口大小 |
| `BROWSER_DEDUP_THRESHOLD` | 2 | 同指纹重复多少次后拒绝 |
| `BROWSER_RECENT_STEPS_IN_PROMPT` | 8 | prompt 中注入的最近步数 |
| `BROWSER_MAX_PLAN_STEPS` | 8 | 初始 plan 最大 step 数 |
| `BROWSER_MAX_REPLANS` | 2 | 单任务最多 replan 次数 |
| `BROWSER_STEP_STUCK_THRESHOLD` | 4 | 单个 step 卡多少次触发 replan |
| `BROWSER_PLAN_MEMORY_ENABLED` | false | P3 长期记忆开关 |

## 新增 / 修改文件一览

**新增：**
- `agents/browser_task_plan.py`
- `utils/browser_plan_memory.py`（P3）
- `prompts/browser_act.txt`
- `prompts/browser_task_plan.txt`
- `prompts/browser_step_advance.txt`
- `tests/test_browser_step_dedup_unit.py`
- `tests/test_browser_task_plan_unit.py`

**修改：**
- `agents/browser_decision.py`（指纹、plan 注入、_plan_next_action 重构）
- `agents/browser_agent.py`（plan 生命周期 hook）
- `agents/web_worker.py`（plan 状态透传）
- `config/settings.py`（新增配置项）
- `prompts/browser_unified_plan.txt`（P0 增 repeated_actions；P2 删除）
- `prompts/browser_page_assessment.txt`（P0 增 repeated_actions；P2 删除）
- `prompts/browser_action_decision.txt`（P0 增 repeated_actions；P2 删除）

## 落地顺序

1. **P0 指纹去重**（立刻可见效果，风险低）
2. **P2 Prompt 合一**（和 P0 紧接，砍掉冗余后 P1 注入 plan 字段更干净）
3. **P1 任务级 Plan**（架构性改动，在 P0/P2 之上）
4. **P3 长期记忆**（可选，视效果）

## 风险与回滚

- P0：指纹误判导致正确 action 被拒 → 用 `BROWSER_DEDUP_THRESHOLD` 调大、或 env 关闭（设为极大值等效关闭）。
- P1：plan 生成 prompt 不稳定 → 保留"无 plan 模式"开关 `BROWSER_PLAN_ENABLED`（默认 true，异常时可回退）。
- P2：单 prompt 回归可能触发 prompt 回退 → 保留旧三份 prompt 文件至 P2 上线 2 周后再物理删除。
- P3：历史 plan 污染 → seed 只作参考，LLM 可拒绝；保留关闭开关。
