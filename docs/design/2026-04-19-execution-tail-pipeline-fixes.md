# 执行收尾管线修复方案 (AdaptiveSkip / 数据污染 / 答案缺失)

> Status: completed
> Created: 2026-04-19
> Last updated: 2026-04-19

## 0. 背景

2026-04-19 排查 `Hermes Agent 是否开源?如果开源给我找到他的源码链接` 这条日志，
发现"网页操作本身成功"但"最终输出质量很差"——问题不在浏览器决策层，而在
**执行收尾管线**(批次完成→Validator→Replanner→Finalizer 这一段)。

日志关键事实：

1. `TaskExecutor` 执行 `task_1:web.smart_extract` 成功
2. `AdaptiveReroute` 判定 goal satisfied → `Skipped 1 remaining task(s)`
3. 紧接着 `Validator FAIL: task_2 — ['result 为 None']`
4. `Replanner` 第一次重规划 → 启动 `browser.interact`
5. BrowserAgent 一步到位 `goal_satisfied=true`，LLM `reason` 里直接给出
   `https://github.com/nousresearch/hermes-agent`
6. Finalizer 输出却是一张 12 行 SERP 表格,第 1 行是整页 `page_main_text`
   大块文本(含 DuckDuckGo 导航/广告),源码链接埋在第 4 行无格式项里
7. 开头还能看到两次 `ChromaMemory Search memory` 日志

整条链路表现为：**执行成功了但收尾把成果糟蹋了**——(a) 跑了一次完全不必要的
浏览器重规划浪费 ~5 分钟 + 多次 reasoner 调用；(b) 有答案但没呈现给用户。

本文档列 **4 个子项 F1–F4**，每项给 **问题 → 设计 → 实现步骤(带文件路径) →
新增配置 → 测试 → 回滚**。

### 优先级与落地顺序

| 优先级 | 子项 | 工程量 | 依据 |
|---|---|---|---|
| P0 | F1 AdaptiveSkip↔Validator 状态同步 | 10 行 | 直接触发了不必要的 replan 分支，每次浪费数分钟 + 数次推理 LLM |
| P0 | F2 page_main_text 数据污染 | 30 行 | 用户可见的数据质量问题，污染所有 browser 收尾输出 |
| P1 | F3 Finalizer 直答字段 (answer_text) | ~80 行 | 用户问"是否开源+源码链接"，最终给的是 SERP 清单，未作答 |
| P2 | F4 Router 重复 memory 搜索 | ~20 行 | 每次多一次 embedding，不致命 |

### 落地进度一览

| 编号 | 名称 | 状态 | 默认开关 |
|---|---|---|---|
| F1 | AdaptiveSkip ↔ Validator 状态同步 | ✅ 已落地 | — (纯 bug 修复) |
| F2 | page_main_text 数据污染治理 | ✅ 已落地 | — (源头抑制 + 下游过滤) |
| F3 | Finalizer 直答字段(answer_text 透传) | ✅ 已落地 | `BROWSER_ANSWER_TEXT_ENABLED=true` / `FINALIZER_ANSWER_FIRST=true` |
| F4 | Router 重复 memory 搜索合并 | ✅ 已落地 | `MEMORY_QUERY_CACHE_ENABLED=true` |

---

## F1. AdaptiveSkip ↔ Validator 状态同步 (P0)

### F1.1 问题

`core/graph_utils.py:234-250` `apply_adaptive_skip`：

```python
def apply_adaptive_skip(state: OmniCoreState) -> OmniCoreState:
    for task in task_queue:
        if str(task.get("status", "")) == "pending":
            task["status"] = "completed"
            task.setdefault("result", {})                           # ← 关键 bug
            if isinstance(task["result"], dict):
                task["result"]["skipped_by_adaptive_reroute"] = True
            skipped_count += 1
```

`setdefault` 只在**缺少 key** 时才写入默认值。若上游 `task_planner` 把 pending
task 预置为 `{"result": None, ...}`(目前确实是这么做的)，`setdefault` 返回的是
既有的 `None` 并 **不覆盖**，随后 `isinstance(None, dict)` 为 False，
`skipped_by_adaptive_reroute` 标记根本没落地。

紧接着 `agents/validator.py:101-114`：

```python
def validate_task(self, task):
    result = task.get("result")
    if result is None:
        return {"passed": False, "failure_type": "unknown", "issues": ["result 为 None"]}
```

Validator 遍历所有 status=completed 的 task，看到 `result is None` 直接判 FAIL，
触发 `Replanner` 兜底。**被"跳过"的 task 其实是主动放弃的，但在 Validator 眼里
等同于失败**。

观测到的级联代价：

- 一次不必要的 deepseek-chat Replanner 调用
- 一次完整 `browser_agent` 生命周期(acquire context / LLM plan /
  deepseek-reasoner page_assessment / 快照 + 视觉调用 / release)
- 日志中两次 `LLM 异步调用开始: deepseek-reasoner`
- 用户等待时间从~30s 膨胀到 ~5 分钟

### F1.2 设计

双保险修复，两侧都改：

**侧一 (写入侧)**：`apply_adaptive_skip` 强制覆写 `result` 为 dict，不依赖
`setdefault`。语义上"被跳过"的 task 应该有一个明确的 result shape，下游
(Validator / Finalizer / Critic / episode_store) 都可以判定。

**侧二 (读取侧)**：`validator.validate_task` 在 `result is None` 判定之前，先查
`task.get("skipped_by_adaptive_reroute")` 或结构化 `task["result"]["skipped_by_adaptive_reroute"]`，
命中则直接 `passed=True, issues=["skipped"]` 放行。双保险的好处：即便未来有别的
路径把 task 强制置为 completed 且 result=None，validator 也能防御性放行
**明确标注 skip** 的 task。

### F1.3 实现步骤

1. 改 `core/graph_utils.py:234-250` `apply_adaptive_skip`：

   ```python
   for task in task_queue:
       if str(task.get("status", "")) == "pending":
           task["status"] = "completed"
           # 强制覆写,而非 setdefault —— 上游可能预置 result=None
           existing = task.get("result")
           if not isinstance(existing, dict):
               task["result"] = {}
           task["result"]["skipped_by_adaptive_reroute"] = True
           task["result"]["success"] = True   # 明确告诉 validator 这是主动跳过
           task["skipped_by_adaptive_reroute"] = True  # 顶层冗余,方便 validator 扫
           skipped_count += 1
   ```

2. 改 `agents/validator.py:101-114` `validate_task`，在函数开头加 early return：

   ```python
   if task.get("skipped_by_adaptive_reroute") or (
       isinstance(task.get("result"), dict)
       and task["result"].get("skipped_by_adaptive_reroute")
   ):
       return {"passed": True, "failure_type": None, "issues": ["skipped_by_adaptive_reroute"]}
   ```

3. 改 `core/finalizer.py`：聚合 completed 任务的 result 时，遇到
   `skipped_by_adaptive_reroute=True` 的 task 不要把它算进"产出"统计，
   避免 finalize 把一个空壳算成有效任务。

4. 改 `core/critic.py` (若 critic 会对 task 做评分)：跳过被 skip 的 task，
   不计入打分分母。

### F1.4 新增配置

**无**。这是纯 bug 修复，不需要开关。

### F1.5 测试

新增 `tests/test_adaptive_skip_validator_unit.py`：

- 场景 1：task_queue 中 pending task 预置 `{"result": None}`，调用
  `apply_adaptive_skip` 后断言 `task["result"]["skipped_by_adaptive_reroute"] is True`、
  `task["result"]["success"] is True`
- 场景 2：同样预置 `{"result": None}`，跳过后走 `validator.validate_task`，断言
  `passed=True` 且 `failure_type is None`
- 场景 3：task 没有 result key(`KeyError`)，apply_adaptive_skip 后 result
  存在且带标记
- 场景 4：task 预置 `{"result": {"existing_field": 1}}`，跳过后 `existing_field`
  保留、`skipped_by_adaptive_reroute` 追加而非覆盖

### F1.6 回滚

直接 `git revert`。这是纯正向修复，无开关、无兼容分支。

---

## F2. page_main_text 数据污染治理 (P0)

### F2.1 问题

`agents/browser_agent.py` 在 4 个地方 (`2531`, `2558`, `2837`, `2896`) 无条件调用：

```python
_merge_new_data([{"text": main_text, "source": "page_main_text"}])
```

`main_text` 是当前页面的主文本区块(SERP 页可能超过 2KB，包含导航菜单、
搜索建议、广告、相关搜索等噪声)，被原样塞进 `result.data` 列表的**第一条**，
下游 Finalizer 把它渲染成表格第一行的一个巨大 cell。

日志中最终输出的第一条就是这种块：

```
| 1 | DuckDuckGo 打开菜单 - 全部 - 图片 - 新闻 - 视频 - 更多 地图 购物 -
Search Assist - Duck.ai - 搜索设置 受保护 不限地区 ... (~2KB 纯文本) | ... |
page_main_text |
```

这条冗余数据把：

1. **Finalizer 表格**污染掉(markdown 表格里含超长单 cell)
2. **Artifact 存储**膨胀(`artifact_store` 写了一份没人会看的 2KB 文本)
3. **后续 router/planner 的 RAG 召回**拉低质量(向量会把这段 SERP 噪声
   当作"用户相关记忆"召回回来)

设计初衷(`_merge_new_data(page_main_text)`)是**兜底**：怕结构化 extract 失败时
至少有一段正文可用。但当前没有"已有结构化结果则跳过兜底"的分支。

### F2.2 设计

两层改动：

**策略一 (源头抑制)**：在 browser_agent 的 DONE / EXTRACT 分支，改为"仅当
accumulated_data 为空时才回退到 page_main_text"——即把 page_main_text
从**总是 append** 改为**fallback-only**。

**策略二 (下游过滤)**：新增 `utils/result_sanitizer.py`，在 Finalizer 读取
`result.data` 前做一次清洗：

- 若 data 列表中存在 `source != "page_main_text"` 的条目，丢弃所有
  `source == "page_main_text"` 条目
- 若整个 data 只剩 page_main_text，保留但截断到 `MAX_FALLBACK_TEXT_LEN` 字符
  (默认 800)，并标记 `truncated=True`

两层都做的原因：策略一解决"写入"，策略二防御已经落地的历史数据(artifact 回放时)
或别的路径写入的噪声。

### F2.3 实现步骤

1. 改 `agents/browser_agent.py:2525-2562`  (DONE 分支) 与 `2830-2900` (两处 EXTRACT)：

   ```python
   if action.action_type == ActionType.DONE:
       data = await self._extract_data_for_intent(task_intent)
       _merge_new_data(data)
       # page_main_text 降级为 fallback,仅当结构化数据完全为空时使用
       if not accumulated_data:
           snapshot = self._last_semantic_snapshot or {}
           main_text = self._get_snapshot_main_text(snapshot)
           if main_text and len(main_text) >= 50:
               _merge_new_data([{
                   "text": main_text[:800],
                   "source": "page_main_text_fallback",
                   "truncated": len(main_text) > 800,
               }])
   ```

   四处 call site 都按这个模式改；`page_main_text` 改名为
   `page_main_text_fallback` 方便日后 grep。

2. 新增 `utils/result_sanitizer.py`：

   ```python
   FALLBACK_SOURCES = {"page_main_text", "page_main_text_fallback"}

   def sanitize_browser_data(data: list, max_fallback_len: int = 800) -> list:
       if not isinstance(data, list) or not data:
           return data
       has_structured = any(
           isinstance(item, dict) and item.get("source") not in FALLBACK_SOURCES
           for item in data
       )
       if has_structured:
           return [item for item in data
                   if not (isinstance(item, dict) and item.get("source") in FALLBACK_SOURCES)]
       # 仅 fallback,截断
       cleaned = []
       for item in data:
           if isinstance(item, dict) and isinstance(item.get("text"), str):
               text = item["text"]
               if len(text) > max_fallback_len:
                   item = dict(item)
                   item["text"] = text[:max_fallback_len]
                   item["truncated"] = True
           cleaned.append(item)
       return cleaned
   ```

3. 改 `core/finalizer.py`：渲染 browser task 的 `result.data` 之前调用
   `sanitize_browser_data(data)`。具体位置：finalize 主循环里拼接
   `result["data"]` 进 markdown 的那一段(搜 `result.get("data")` 或
   `for item in data` 能定位)。

4. 改 `agents/browser_agent.py` 的 return payload (`2540-2548`)：先对
   `accumulated_data or data` 跑一遍 sanitize 再 return，保证
   `artifact_store` 也写清洗后的版本。

### F2.4 新增配置

```
BROWSER_FALLBACK_TEXT_MAX_LEN=800       # page_main_text fallback 截断阈值
BROWSER_FALLBACK_TEXT_MIN_LEN=50        # 低于此长度不记
```

统一加到 `config/settings.py`，不走 env 直接写死也可(因为这是纯展示阈值)。

### F2.5 测试

新增 `tests/test_result_sanitizer_unit.py`：

- `has_structured=True` 时丢弃 page_main_text / page_main_text_fallback
- `has_structured=False` 时保留 fallback 并截断到阈值
- data 中全是 str 而非 dict 的退化情况不崩
- 空 list / None 直接返回原值

扩展 `tests/test_browser_agent_unit.py`：mock action_type=DONE，
已有结构化 cards → 断言 return payload 的 data 里不含 `page_main_text_fallback`；
反之结构化空 → 至少 1 条 fallback 且长度 ≤ 800。

### F2.6 回滚

sanitize 是纯过滤函数，改 Finalizer 那一行回退即可。browser_agent 侧
把 `if not accumulated_data:` 包裹去掉即回到旧行为。

---

## F3. Finalizer 直答字段 (answer_text 透传) (P1)

### F3.1 问题

日志中 `browser_perception` 做 page_assessment 后，LLM 明确回了：

```json
{
  "page_relevant": true,
  "goal_satisfied": true,
  "reason": "The visible data explicitly states that Hermes Agent is an
  open-source AI agent framework built by Nous Research and includes a link
  to its GitHub repository (https://github.com/nousresearch/hermes-agent),
  which contains the source code.",
  "evidence_indexes": [0, 1],
  "confidence": 0.95,
  "action": {"type": "extract", ...}
}
```

这段 `reason` 已经是**对用户问题的直接回答**。但：

1. 它只存在于 decision 层内部 payload，没有向上透传到 `result`
2. Finalizer 只看 `result.data` 列表，不读 reason，于是输出只剩 SERP 堆砌
3. 用户问"是否开源 + 源码链接"，得到的却是 12 行 SERP 摘要，必须自己在表格里找

### F3.2 设计

在 browser_agent 的 return payload 新增两个字段并透传到 Finalizer：

- `answer_text`: 来自 page_assessment 的 `reason` 字段(若存在且 goal_satisfied=True)
- `answer_citations`: 来自 `evidence_indexes` 映射回 data 条目的 url 列表
  (方便 Finalizer 在答案后附"来源"行)

Finalizer 渲染策略：

- 若 `answer_text` 存在，作为输出首段**直答**(前置到 SUCCESS 面板首行)
- 然后附 `answer_citations` 的 1-3 条 url
- 原有数据表格作为"详细资料"降级到第二屏(可折叠/省略)

这样用户拿到的第一眼信息是：

```
Hermes Agent 是开源的,由 Nous Research 构建。源码:
https://github.com/nousresearch/hermes-agent

详细资料(12 条):
| 1 | ...
```

而不是直接一张表让他自己翻。

### F3.3 实现步骤

1. 改 `agents/browser_decision.py`：`_decide_action_with_llm` /
   `_act_with_llm` 的返回结构新增 `assessment_reason` / `evidence_indexes`
   字段(从 LLM response 里提取，已经有的结构不改)

2. 改 `agents/browser_agent.py` 的 `_execute_step` DONE/EXTRACT 分支
   (`2525-2570`)，把决策层的 reason/evidence 捕获到 self 上的
   `_last_assessment_reason` / `_last_evidence_indexes`；DONE 分支 return
   payload 增加：

   ```python
   return {
       "status": "exit",
       "result": {
           ...,
           "answer_text": self._last_assessment_reason or "",
           "answer_citations": self._resolve_evidence_urls(
               self._last_evidence_indexes, accumulated_data
           ),
       },
   }
   ```

   `_resolve_evidence_urls` 按 index 去 accumulated_data 里找 `url` 字段。

3. 改 `core/finalizer.py`：生成 SUCCESS 面板文本时，先检查 browser task
   `result.answer_text`，非空则作为首段；然后渲染数据表。

4. 改 Finalizer 对应 prompt(若走 LLM 总结路径)：prompt 里新增说明
   "若上游提供 answer_text 字段，请以它为主回答，数据仅作证据支撑"。

### F3.4 新增配置

```
BROWSER_ANSWER_TEXT_ENABLED=true        # 是否透传 answer_text
FINALIZER_ANSWER_FIRST=true             # finalize 是否把直答前置
FINALIZER_MAX_CITATIONS=3
```

### F3.5 测试

- `tests/test_browser_answer_text_unit.py`：mock decision 返回带
  `reason`，断言 browser_agent return payload 的 `result.answer_text` 非空
- `tests/test_finalizer_answer_first_unit.py`：mock task result 带
  `answer_text`，断言最终输出首段包含 answer_text 且在数据表之前

### F3.6 回滚

`BROWSER_ANSWER_TEXT_ENABLED=false` / `FINALIZER_ANSWER_FIRST=false`，
Finalizer 直接跳过新分支，回到旧渲染。

---

## F4. Router 重复 memory 搜索合并 (P2)

### F4.1 问题

日志开头看到：

```
[Agent: ChromaMemory] Search memory -> Hermes Agent是开源的吗?...
[Agent: Router] RAG context injected -> 3 items
[Agent: Router] 开始分析用户意图 -> Hermes Agent是开源的吗?...
[Agent: ChromaMemory] Search memory -> Hermes Agent是开源的吗?...
```

**同一 query 搜了两次**。推断路径：

1. 第一次：Router 入口前由 `graph_nodes` 或 `rag_injector` 触发的前置
   RAG 注入，`knowledge_context` 参数
2. 第二次：Router 内部 `analyze_intent` 又调了一次 memory search
   生成 `related_history`

两段代码各自按需取用，但查询语句 **完全相同**，embedding + chroma 检索做两次。
每次 embedding 调用约 0.5-2s，这是纯浪费。

### F4.2 设计

把 memory search 做成**单次查询 + 多 consumer 复用**：

- 在 `graph_nodes` 的 Router 前置阶段做**一次**查询，结果存入
  `state["memory_search_cache"] = {"query": ..., "results": [...], "ts": ...}`
- `Router.analyze_intent` 先查 state cache，命中(query 相同且 ts 在 60s 内)
  则复用，miss 才重新查
- 其他可能消费者(planner RAG hint / critic background)也从同一 cache 取

### F4.3 实现步骤

1. 新增 `utils/memory_query_cache.py`：`get_or_search(state, query, top_k)`
2. 改 `core/graph_nodes.py` 或 `core/rag_injector.py`(找当前做前置 RAG 注入
   的位置)：调 `get_or_search` 而非直接 `chroma.search`，并把结果回写 state
3. 改 `core/router.py:analyze_intent`：构造 `related_history` 前先从 state
   cache 取；若 cache 命中且 query 相同，跳过 memory search；否则 fallback
   到旧路径

### F4.4 新增配置

```
MEMORY_QUERY_CACHE_ENABLED=true
MEMORY_QUERY_CACHE_TTL_SEC=60      # 同一 session 内 60s 内同 query 复用
```

### F4.5 测试

`tests/test_memory_query_cache_unit.py`：

- 同 query 连续 2 次调用 → 第 2 次直接返回缓存
- ts 过期 → 重新查询
- 不同 query → 各自查询

### F4.6 回滚

关 `MEMORY_QUERY_CACHE_ENABLED`，路径 fallback 回每次都查。

---

## 附录 A. 与已有方案的关系

| 已有方案 | 本文增量 |
|---|---|
| Validator 硬规则验证(`agents/validator.py`) | F1 新增 skip 状态识别 |
| AdaptiveReroute(`core/graph_utils.py`) | F1 修复 result 覆写 bug |
| Browser 视觉缓存 B3 / page_assessment | F3 复用已有 reason / evidence 字段,向上透传 |
| 记忆实体倒排 A2 / 偏好学习 A5 | F4 在查询侧加缓存,不改存储 |
| 2026-04-18 E1 Prompt Injection `<UNTRUSTED>` | F2 丢弃 page_main_text 进一步降低注入面(主文本块正是 E1 包裹对象) |

## 附录 B. 联动的日志观测点

修复后期望观测到：

- `Skipped N remaining task(s)` 之后**不再**有
  `Validator FAIL: taskN — ['result 为 None']`
- browser task 的 Finalizer SUCCESS 面板首段是一句直接回答(而非直接放 SERP 表)
- browser result.data 中 `page_main_text` / `page_main_text_fallback` 条目
  在有结构化数据时不出现
- 同一 Router 调用链内 `ChromaMemory Search memory` 只出现一次

## 附录 C. 不做的事

- **不改 Router 子任务拆分策略**：本次日志中 router 拆出 2 个子任务(web.smart_extract + ?)
  不是问题本质,问题在 task_2 被跳过后没被正确识别
- **不改 Replanner 策略**：一旦 F1 修好,这条路径根本不会进入 Replanner
- **不改 browser 决策本身**：决策层已经给出正确答案,瓶颈在收尾
- **不加成本硬上限**：E3-lite(2026-04-19 已完成)已覆盖 token 监控,此类浪费
  通过 F1 源头杜绝而非事后拦截
