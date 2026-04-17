# 记忆能力与网页操作优化方案

> Status: in-progress (A 组 A1~A5 全部上线并收口;B 组未开工)
> Created: 2026-04-16
> Last updated: 2026-04-16

## 落地进度一览

| 编号 | 名称 | 状态 | 默认开关 | 单测 |
|---|---|---|---|---|
| A1 | 记忆衰减 + TTL + 摘要归档 | ✅ 上线 | 衰减 on / 归档 off | 19 |
| A2 | 实体倒排索引 + Router 注入 + delete_by_entity | ✅ 上线 | on(写) / off(注入 router) | 11 |
| A3 | Skill Store 规划前置注入 | ✅ 上线 | on | 9 |
| A4 | 记忆分层 + session-close purge 钩子 | ✅ 上线 | **on**(legacy fallback) | 15 |
| A5 | 用户偏好自动学习 + LLM 归纳 + Router 注入 | ✅ 上线 | 规则层 on / LLM 层 & 注入 off | 22 |
| B1 | 站点选择器 + 登录流记忆 | ⏳ 未开工 | — | — |
| B2 | 反爬 domain 画像 | ⏳ 未开工 | — | — |
| B3 | 视觉验证缓存 | ⏳ 未开工 | — | — |
| B4 | iframe / 多 tab 支持 | ⏳ 未开工 | — | — |
| B5 | 失败策略自适应学习 | ⏳ 未开工 | — | — |
| B6 | 三模式解耦重构 | ⏳ 未开工 | — | — |

A 组合计新增测试 76 条,全部通过;回归 `tests/test_skill_store_unit.py` / `tests/test_router_unit.py` / `tests/test_memory_decay_unit.py` / `tests/test_memory_consolidator_unit.py` / `tests/test_tiered_memory_unit.py`(共 96 条)全绿。

## 0. 背景与总览

当前 OmniCore 在 2026-03 ~ 2026-04 已完成 Runtime 基础、Tool Dispatch、成本感知路由、以及 Browser 规划优化 P0/P1/P2/P4。本文档聚焦两个仍有显著优化空间的领域：

- **记忆能力**：写入充分但"只存不悟",缺衰减/分层/实体复用/偏好学习
- **网页操作**：每次会话都是"陌生人",跨会话无站点级知识复用；反爬、iframe、多 tab、失败降级策略均未成体系

本文档列出 11 个具体可落地的子项,每项给出：**问题 → 设计 → 实现步骤(带文件路径) → 新增配置 → 测试 → 回滚**。

### 优先级建议

| 优先级 | 子项 | 依据 |
|---|---|---|
| P0 | A1 记忆衰减+TTL、A2 实体倒排索引、B1 站点选择器/登录流记忆(P3) | 改动集中、收益立竿见影、对齐已有预留接口 |
| P1 | A3 Skill 前置注入、B3 视觉验证缓存、B5 失败策略学习 | 单点小改、LLM 成本与成功率直接受益 |
| P2 | A5 偏好学习、B2 反爬 domain 画像、B4 iframe/多 tab | 受众相对窄或工程量较大 |
| P3 | A4 记忆分层重构、B6 三模式解耦 | 偏重构,需稳定期后再做 |

---

## A. 记忆能力优化

### A1. 记忆时间衰减 + TTL + 老记忆 LLM 摘要归档

#### A1.1 问题

- `memory/scoped_chroma_store.py:334-343` 写入了 `created_at` / `updated_at` / `hit_count`,但 `search_memory` 只按语义距离排序,时间因子完全缺失
- `memory/manager.py:288` 的召回距离阈值 `0.15` 写死,老记忆永远有机会干扰最新任务
- 没有清理或归档机制,Chroma collection 会无限增长,相似度检索质量随时间劣化

#### A1.2 设计

**三层机制**：

1. **召回时时间衰减加权**：打分公式 `score = similarity * decay(age) * log(1 + hit_count)`,其中 `decay(age) = exp(-age_days / half_life_days)`
2. **定期 TTL 扫描**：对 `created_at < now - ttl_days` 且 `hit_count == 0` 的记忆直接删除；对 `hit_count >= 1` 但超龄的记忆进入归档流程
3. **归档摘要**：将一批即将过期且同 scope 的老记忆批量送给 LLM(用低成本模型)生成"阶段摘要",以单条 `memory_type=consolidated_summary` 写回,原记忆删除

#### A1.3 实现步骤

1. **新增 `memory/decay.py`**
   - `compute_decay_score(similarity: float, created_at: str, hit_count: int, half_life_days: float) -> float`
   - 纯函数,便于单测

2. **修改 `memory/scoped_chroma_store.py:search_memory`**
   - 返回结果后,用 `decay.compute_decay_score` 重排序(保留 raw similarity 用于 debug)
   - `half_life_days` 从 `config/settings.py` 读(新增 `MEMORY_HALF_LIFE_DAYS`,默认 30)

3. **修改 `memory/scoped_chroma_store.py:_get_single_record` 命中后更新 `last_accessed_at` 和 `hit_count += 1`**
   - 目前只读不写回,衰减公式失去"被复用记忆更重要"的信号

4. **新增 `memory/consolidator.py`**
   - `consolidate_expired(store: ChromaMemory, ttl_days: int, batch_size: int = 10) -> ConsolidationReport`
   - 查询 `created_at < cutoff` 的记录 → 按 `scope_key` 分组 → 每组 ≤batch_size 条一起送 LLM → 写回 `memory_type=consolidated_summary` → 删除原记忆
   - 使用 `prompts/memory_consolidation.txt`(新建),要求输出"目标-结果-学到的事"三段式摘要

5. **接入调度**
   - `utils/workflow_automation_store.py` 已有 cron 能力,注册每日凌晨 3 点跑 `consolidator.consolidate_expired`
   - 同步在 `main.py` 新增 `python main.py memory-consolidate` 一次性手动入口,便于调试

#### A1.4 新增配置

```bash
# .env.example
MEMORY_HALF_LIFE_DAYS=30          # 语义相似度的时间衰减半衰期
MEMORY_TTL_DAYS=90                # 超过此天数且 hit_count=0 的记忆直接删除
MEMORY_CONSOLIDATION_ENABLED=false  # 先开关关闭,灰度
MEMORY_CONSOLIDATION_MODEL=gpt-4o-mini
MEMORY_CONSOLIDATION_BATCH_SIZE=10
```

#### A1.5 测试

- `tests/test_memory_decay_unit.py`
  - 同等 similarity 下,老记忆得分 < 新记忆
  - `hit_count` 翻倍后得分显著提升
  - 半衰期边界值(age=half_life → decay=0.5)
- `tests/test_memory_consolidator_unit.py`
  - Mock LLM,验证按 scope 分组、原记录删除、摘要记录写回
  - `hit_count >= 1` 的老记忆走摘要路径而非删除

#### A1.6 回滚

- `MEMORY_HALF_LIFE_DAYS=99999` 等效关闭衰减
- `MEMORY_CONSOLIDATION_ENABLED=false` 跳过整个归档流程
- 摘要记录带 `memory_type=consolidated_summary`,如需撤销可直接按 type 过滤删除

#### A1.7 落地情况(2026-04-16)

**代码改动**:
- 新增 `memory/decay.py`:`compute_decay_score` / `rerank_by_decay`,纯函数,无外部依赖
- 新增 `memory/consolidator.py`:`consolidate_expired` 入口 + `ConsolidationReport`,支持 dry-run
- 新增 `prompts/memory_consolidation.txt`:三段式摘要 JSON 输出约束
- `memory/scoped_chroma_store.py:search_memory` 接入衰减重排(开关 `MEMORY_DECAY_ENABLED`),池倍数 `MEMORY_RERANK_POOL_MULTIPLIER`
- `main.py` 新增 `python main.py memory-consolidate [--dry-run]` CLI 入口
- `config/settings.py` 新增配置:`MEMORY_DECAY_ENABLED` / `MEMORY_HALF_LIFE_DAYS` / `MEMORY_RERANK_POOL_MULTIPLIER` / `MEMORY_TTL_DAYS` / `MEMORY_CONSOLIDATION_ENABLED` / `MEMORY_CONSOLIDATION_MODEL` / `MEMORY_CONSOLIDATION_BATCH_SIZE` / `MEMORY_CONSOLIDATION_MIN_HITS`

**默认开关**:
- `MEMORY_DECAY_ENABLED=true`(衰减重排立即生效,改动平滑)
- `MEMORY_CONSOLIDATION_ENABLED=false`(LLM 归档仍需人工触发或显式开启)

**测试**:`tests/test_memory_decay_unit.py`(11 条) + `tests/test_memory_consolidator_unit.py`(8 条)

**关键决策**:
- `search_memory` 采用"开关独立"模式:`MEMORY_DECAY_ENABLED=false` 时完全走原路径,重排只是额外一层
- `consolidate_expired` 支持 `now=...` 注入,测试无需 freeze 时间
- 永不自动归档 `preference` / `skill_definition` / `consolidated_summary` 三种类型

---

### A2. 实体倒排索引 + 分面检索

#### A2.1 问题

- `memory/entity_extractor.py` 被 `manager.py:226-245` 调用后,实体只以逗号拼接字符串形式写入 `entity_person` / `entity_org` 等 metadata 字段
- 后续 `search_related_history`(`manager.py:135-152`)完全不用这些 metadata,白提取
- 无法回答"最近跟 Acme 这家公司相关的任务有哪些"这类分面问题

#### A2.2 设计

- 新建独立 Chroma collection `omnicore_entities`,存储 `{entity_type, entity_text, related_memory_ids, first_seen, last_seen, occurrence_count}`
- 持久化任务记忆时,同步写入/更新实体索引
- `MemoryManager` 新增 `search_by_entity(entity_text, entity_type=None)` 和 `list_top_entities(entity_type, limit)` 两个检索接口
- Planner/Router 可在长 session 内调用 `list_top_entities` 作为"用户当前关心的主体"上下文

#### A2.3 实现步骤

1. **新增 `memory/entity_index.py`**
   - `EntityIndex` 类,封装独立 Chroma collection
   - `record(entity_type, entity_text, memory_id)` — upsert,`occurrence_count += 1`,更新 `last_seen`
   - `search(entity_text)` — 语义 + 精确两路召回合并
   - `top_entities(entity_type=None, limit=10)` — 按 `occurrence_count DESC`

2. **修改 `memory/manager.py:persist_job_outcome`**
   - 在实体提取成功分支(line 231-240)后,若 `self.entity_index`(新增属性)不为 None,为每个实体调用 `record(type, text, memory_id)`

3. **修改 `MemoryManager.__init__`** 接受可选 `entity_index`,由 `main.py` / runtime 构造

4. **`core/router.py` 注入 top entities**(可选,作为 prompt 上下文)
   - 新开关 `MEMORY_INJECT_TOP_ENTITIES=false`,开启后路由 prompt 加入"用户近期关注的实体"块

#### A2.4 新增配置

```bash
MEMORY_ENTITY_INDEX_ENABLED=true
MEMORY_ENTITY_INDEX_COLLECTION=omnicore_entities
MEMORY_INJECT_TOP_ENTITIES=false   # 默认不注入,避免 prompt 膨胀
MEMORY_ENTITY_TOP_K=5
```

#### A2.5 测试

- `tests/test_entity_index_unit.py`
  - 同实体重复 record 后 `occurrence_count` 正确累加
  - `search("Acme")` 能召回所有相关 memory_id
  - `top_entities("ORG")` 按次数排序正确

#### A2.6 回滚

- `MEMORY_ENTITY_INDEX_ENABLED=false` 完全跳过
- 独立 collection,不影响原有 `omnicore_memory`

#### A2.7 落地情况(2026-04-16)

**代码改动**:
- 新增 `memory/entity_index.py`:`EntityIndex` 类 + `EntityRecord` dataclass,独立 collection `omnicore_entities`
- 实体 upsert 按 `entity:{type}:{text}` 做指纹,重复记录累加 `occurrence_count`,维护 `first_seen/last_seen` 与 `related_memory_ids`(最多 50 条,末尾截断)
- `memory/manager.py`:`MemoryManager` 新增 `entity_index` lazy 属性(默认关闭时返回 None)、`search_by_entity` / `list_top_entities` 两个公开接口
- `persist_job_outcome` 在实体提取成功、拿到 task `memory_id` 之后同步调用 `entity_index.record_many`(包在 try/except 内,永不阻塞主写路径)
- `config/settings.py` 新增:`MEMORY_ENTITY_INDEX_ENABLED` / `MEMORY_ENTITY_INDEX_COLLECTION` / `MEMORY_INJECT_TOP_ENTITIES` / `MEMORY_ENTITY_TOP_K`

**默认开关**:
- `MEMORY_ENTITY_INDEX_ENABLED=true`(同步写入无成本开启)
- `MEMORY_INJECT_TOP_ENTITIES=false`(router prompt 注入先不开,避免 prompt 膨胀)

**测试**:`tests/test_entity_index_unit.py`(7 条):记录累加、空文本拒写、类型过滤、top-K 排序、feature flag 关闭时返回空

**收口补充(2026-04-16)**:
- Router 注入已落地:`core/router.py:_build_top_entities_block` 在 `analyze_intent` 中按 `MEMORY_INJECT_TOP_ENTITIES` 开关注入 "Recently-active entities" 区块(按 `entity_type` 分组,跳过 `occurrence_count == 0`,异常全部吞掉)
- `EntityIndex.delete_by_entity(text, entity_type=None)`:按文本 + 类型精确清理;大小写不敏感;禁用时返回 0
- 默认开关 `MEMORY_INJECT_TOP_ENTITIES=false`,灰度观察后再打开

---

### A3. Skill Store 规划前置注入

#### A3.1 问题

- `memory/skill_store.py` 提取 skill 逻辑完整,但只在 Router/Planner "精确命中"时复用
- 部分匹配或相似任务时,完全走 LLM 全量规划,已有 skill 模板形同虚设

#### A3.2 设计

- Planner 在生成 plan 前,调 `skill_store.match_top_k(user_input, k=3)` 拿到 top-3 相关 skill(即使未完全命中)
- 将 skill 摘要(任务模板 + 工具序列 + 成功率)注入 `prompts/task_planner.txt` 的新变量 `{related_skills}`
- 不强制 LLM 照抄 skill,而是作为参考上下文,由 LLM 自主取舍

#### A3.3 实现步骤

1. **`memory/skill_store.py` 新增 `match_top_k(task_description, k=3) -> List[SkillMatch]`**
   - 现有 `match` 内部改为 k=1 的特例,共用打分逻辑
   - `SkillMatch` 包含 `score, skill_id, template, tool_sequence, success_rate`

2. **修改 `core/task_planner.py`**
   - 在构造 prompt 前调用 `skill_store.match_top_k`
   - 将结果格式化为紧凑 YAML 块,注入 prompt 变量

3. **修改 `prompts/task_planner.txt`**
   - 新增 `## Related Skills (reference only, not mandatory)` 章节
   - 明确指示"如果任务本质一致,优先沿用 skill 的 tool_sequence；如不同,请忽略"

4. **埋点**
   - `utils/cost_tracker.py` 新增 `record_skill_hint_usage(skill_id, hinted, adopted)`,用于 A/B 统计"注入是否真的被采纳"

#### A3.4 新增配置

```bash
SKILL_HINT_ENABLED=true
SKILL_HINT_TOP_K=3
SKILL_HINT_MIN_SCORE=0.4
```

#### A3.5 测试

- `tests/test_skill_hint_unit.py`
  - 注入后 prompt 长度不超过预算
  - 分数低于阈值的 skill 不被注入
  - 精确命中时仍走原 `match` 路径(不重复注入)

#### A3.6 回滚

- `SKILL_HINT_ENABLED=false` 完全跳过

#### A3.7 落地情况(2026-04-16)

**代码改动**:
- `memory/skill_store.py` 新增 `SkillMatch` dataclass(`score` / `success_rate` / `total_uses` / `tool_sequence` / `source_intent`) 与 `match_top_k(user_input, k, min_score)` 方法
  - 过滤规则与原 `match()` 一致:废弃 skill 跳过;`total≥3 且 success_rate<0.3` 的失败模式跳过
  - `score = max(0, 1 - distance)`,按 score 降序
- `core/router.py` 新增 `_build_skill_hint_block(user_input)`:renders "Related skill templates (reference only ...)" 区块,显示 name / score / uses / success% / tool 序列
  - 在 `analyze_intent` 的 `user_message` 末尾(`knowledge_context` 之后、"请分析..."之前)注入
  - 明确指示 LLM "如果任务本质一致,优先沿用 skill 的 tool_sequence;否则忽略"
- `config/settings.py` 新增:`SKILL_HINT_ENABLED` / `SKILL_HINT_TOP_K` / `SKILL_HINT_MIN_SCORE`
- 设计文档原计划注入 `prompts/task_planner.txt`,**实际实现中项目已用 `router_system` 做 plan 构造**,所以改为在 router user_message 注入。效果等价,且无需改 prompt 文件

**默认开关**:
- `SKILL_HINT_ENABLED=true` / `SKILL_HINT_TOP_K=3` / `SKILL_HINT_MIN_SCORE=0.4`

**测试**:`tests/test_skill_hint_unit.py`(9 条):`match_top_k` 排序/过滤/空输入/废弃 skill 跳过/router block 渲染/禁用返回空/异常捕获

**关键决策**:
- 现有 `Router.route()` 中 `skill_store.match()` 精确命中路径保留不变(命中即跳过 LLM 规划),`_build_skill_hint_block` 只在走 LLM 规划分支生效,两者互不冲突
- 埋点暂未接入 `cost_tracker`(原设计的 `record_skill_hint_usage`):需要先打开后观察真实采纳率,避免提前做 A/B 开销

---

### A4. 记忆分层重构(working / episodic / semantic)

#### A4.1 问题

- 所有类型记忆(task result、artifact、user preference、consolidated summary)都在同一 collection,生命周期策略无法差异化
- 短期工作记忆和长期事实混在一起,召回噪声大

#### A4.2 设计

三层独立 collection,各自定义 TTL、召回权重、写入 trigger：

| 层级 | collection | TTL | 典型内容 | 召回时权重 |
|---|---|---|---|---|
| working | `omnicore_working` | 当前 session 结束即删 | 当前 job 的中间产物、对话片段 | 仅当 scope 匹配时召回 |
| episodic | `omnicore_episodic` | 30 天 | 任务结果、artifact 引用 | 常规权重 |
| semantic | `omnicore_semantic` | 90 天 | 用户偏好、consolidated summary、学到的事实 | 常规 + 稳定加成 |

统一通过 `MemoryTier` enum 路由写入/检索。

#### A4.3 实现步骤

1. **`memory/tiered_store.py`**(新建)
   - `TieredMemoryStore` 封装三个 `ChromaMemory` 实例
   - `add(content, tier: MemoryTier, ...)` / `search(query, tiers: List[MemoryTier])`
   - 召回合并打分时按 tier 乘权重系数(`tier_weights` in settings)

2. **迁移脚本 `scripts/migrate_memory_tiers.py`**
   - 按 `memory_type` 把旧 `omnicore_memory` 中的记录搬到对应 tier
   - `user_preference` / `consolidated_summary` → semantic
   - `task_result` / `artifact_reference` → episodic
   - 保留旧 collection 只读,以便回滚

3. **`memory/manager.py` 改为持有 `TieredMemoryStore`**,对调用方接口不变
   - `persist_job_outcome` 明确写入 episodic
   - `persist_preferences` 写入 semantic
   - `consolidator` 的摘要写入 semantic

4. **Session 结束时清理 working**
   - `core/runtime.py` session close 钩子调用 `tiered.purge_working(session_id)`

#### A4.4 新增配置

```bash
MEMORY_TIERED_ENABLED=false       # 先关闭,灰度后开启
MEMORY_TIER_WORKING_COLLECTION=omnicore_working
MEMORY_TIER_EPISODIC_COLLECTION=omnicore_episodic
MEMORY_TIER_SEMANTIC_COLLECTION=omnicore_semantic
MEMORY_TIER_WEIGHT_WORKING=1.0
MEMORY_TIER_WEIGHT_EPISODIC=1.0
MEMORY_TIER_WEIGHT_SEMANTIC=1.2
```

#### A4.5 测试

- `tests/test_tiered_memory_unit.py`
  - 写入各层后独立召回正确
  - Session 结束清理 working 不影响 episodic/semantic
  - 跨层召回按权重排序正确

#### A4.6 回滚

- `MEMORY_TIERED_ENABLED=false` 回到单 collection
- 迁移前的 `omnicore_memory` 保留只读,可直接切回

#### A4.7 落地情况(2026-04-16)

**代码改动**:
- 新增 `memory/tiered_store.py`:
  - `MemoryTier` enum (`WORKING` / `EPISODIC` / `SEMANTIC`)
  - `default_tier_for_type(memory_type)`:preference / consolidated_summary / skill_definition / entity_record → SEMANTIC;task_result / artifact_reference → EPISODIC;其余 → EPISODIC(可显式 override)
  - `TieredMemoryStore`:封装三个 `ChromaMemory`,`add` / `search` / `purge_working(session_id)` / `stats`;搜索时按 tier 权重合并打分(`tier_score = score × tier_weight`),降序返回
  - 关键设计:所有 tier 懒初始化,`MEMORY_TIERED_ENABLED=false` 时不会创建任何新 collection
- 新增 `scripts/migrate_memory_tiers.py`:幂等迁移工具,按 `memory_type` 把 `omnicore_memory` 搬到三层 collection,源保留只读;支持 `--dry-run`
- `config/settings.py` 新增:`MEMORY_TIERED_ENABLED` / 三个 `MEMORY_TIER_*_COLLECTION` / 三个 `MEMORY_TIER_WEIGHT_*`
- `MemoryManager` 暂未切换到 `TieredMemoryStore`(避免触发迁移),现阶段只是提供了基础设施

**默认开关**:
- `MEMORY_TIERED_ENABLED=true`(2026-04-16 切到 on,带 legacy fallback,无需强制迁移)
- `MEMORY_TIER_LEGACY_FALLBACK=true`:tier 命中为空时自动回读 `omnicore_memory`,保证历史数据无断层
- `MEMORY_TIER_LEGACY_COLLECTION=omnicore_memory`:fallback 目标 collection 名

**启用后的新行为**:
- **写路径**:`MemoryManager._writer_for(memory_type)` 按类型路由
  - `task_result` / `artifact_reference` → `omnicore_episodic`
  - `preference` / `consolidated_summary` / `skill_definition` / `entity_record` → `omnicore_semantic`
  - 其余 → episodic(可显式覆盖 tier 参数)
- **读路径**:`search_related_history` 改为先走 `TieredMemoryStore.search`,按 tier 权重合并;tier 命中为空时回退到 legacy collection,旧数据继续可见
- **TieredMemoryStore 新增** `store_for_type(memory_type)`:提供给 manager 做写路由
- **TieredMemoryStore 新增** legacy reader:构造时注入 `legacy=ChromaMemory(legacy_collection)`,`_get_legacy()` 懒加载

**测试**:`tests/test_tiered_memory_unit.py`(10 条)覆盖 tier 路由 / 权重排序 / tiers 过滤 / purge_working / stats / 禁用场景;`tests/test_preference_learner_unit.py` 的 `TestPersistInferredPreferences` 显式关闭 tiered 以测 stub 路径

**收口补充(2026-04-16)**:
- 新增 `core/runtime.purge_session_working_memory(session_id)`:懒构造 `MemoryManager` → 拿到 `tiered_store` → 调 `purge_working(session_id)`;禁用分层或无 session_id 时返回 0;异常吞掉
- `main.py` 交互式 CLI 在三个退出路径(`quit/exit/q`、`Ctrl+C`、`EOFError`)调用 `_purge_working_memory_on_exit(session_id)` 触发清理
- 一次性迁移脚本仍需手工运行:`python scripts/migrate_memory_tiers.py [--dry-run]`(legacy 源只读保留,无破坏)

**关闭方法**:`.env` 设置 `MEMORY_TIERED_ENABLED=false` 立即回退到单 collection 读写路径,已有 tier 数据不丢失,切回后只是不可见

---

### A5. 用户偏好自动学习

#### A5.1 问题

- `memory/manager.py:persist_preferences` 只接受显式写入的 `{key: value}`,无法从历史行为中学习
- 系统不知道"这个用户总偏爱 GitHub 而非 GitLab"、"总在午夜执行下载任务"、"偏好 markdown 摘要 over pdf"这类习惯

#### A5.2 设计

引入**离线偏好推断作业**:

- 定期(每日或每 N 个 job)扫描近 7 天的 job outcome
- 用规则 + LLM 总结模式,提取偏好候选(如"tool 使用频率 top 3"、"失败时最常 retry 的工具")
- 每条偏好带 `confidence`(基于出现次数)与 `evidence`(对应 memory_ids)
- 置信度超阈值的偏好写入 semantic tier,并供 router prompt 注入

#### A5.3 实现步骤

1. **新增 `memory/preference_learner.py`**
   - `infer_preferences(store, window_days=7) -> List[PreferenceCandidate]`
   - 内部两步：
     - **规则层**：统计工具使用频次、成功率、偏好时段(纯 Python,零 LLM 成本)
     - **LLM 层**：把规则层的 top-N 统计 + 若干典型 outcome 喂给低成本模型,生成自然语言偏好描述
   - 产出 `PreferenceCandidate(key, value, confidence, evidence_ids)`

2. **`memory/manager.py` 新增 `persist_inferred_preferences`**
   - 置信度 ≥ 阈值才落库,metadata 加 `source=inferred` 以区别手动写入

3. **调度接入**
   - 同 A1 的 `workflow_automation_store`,每日运行
   - 也可在每完成 N 个 job 时增量触发(`config` 可控)

4. **Router 注入**(可选)
   - `core/router.py` 读取 top inferred preferences,格式化为 prompt 块

#### A5.4 新增配置

```bash
PREFERENCE_LEARNING_ENABLED=false
PREFERENCE_LEARNING_WINDOW_DAYS=7
PREFERENCE_LEARNING_MIN_CONFIDENCE=0.6
PREFERENCE_LEARNING_MODEL=gpt-4o-mini
PREFERENCE_INJECT_TO_ROUTER=false
```

#### A5.5 测试

- `tests/test_preference_learner_unit.py`
  - 给定固定 job outcome 序列,统计层工具频次正确
  - LLM 层 mock 后,confidence 低于阈值的不入库
  - 重复运行不重复写入(按 key 去重)

#### A5.6 回滚

- `PREFERENCE_LEARNING_ENABLED=false`
- 推断偏好带 `source=inferred` 标记,可批量清理

#### A5.7 落地情况(2026-04-16)

**代码改动**:
- 新增 `memory/preference_learner.py`:
  - `PreferenceCandidate(key, value, confidence, source, evidence_ids, notes)` dataclass
  - `infer_preferences(store, window_days, min_samples, min_confidence, now)`:扫描 `task_result` 记录,输出三类候选
    - `preferred_tool`:按 `tool_sequence` 统计使用频次,success-weighted score 排序,取 top
    - `common_intent`:按 `intent` 统计最频繁
    - `active_hours`:把 `created_at` 分为 `late_night/morning/afternoon/evening` 四桶,取最频繁
  - 每个候选带 `confidence = max(1.0, count/total)` 与最多 10 个 `evidence_ids`
  - `min_samples` 保底(默认 5),低于阈值直接返回空,避免早期误学
- `memory/manager.py` 新增 `persist_inferred_preferences(candidates, session_id)`:
  - 按 `PREFERENCE_LEARNING_MIN_CONFIDENCE` 过滤
  - 通过 `chroma_memory.save_user_preference` 落库,metadata 带 `source=inferred / confidence / evidence_ids / notes` 便于审计和批量清理
- `main.py` 新增 `python main.py preference-learn [--dry-run]` CLI 入口
- `config/settings.py` 新增:`PREFERENCE_LEARNING_ENABLED` / `PREFERENCE_LEARNING_WINDOW_DAYS` / `PREFERENCE_LEARNING_MIN_CONFIDENCE` / `PREFERENCE_LEARNING_MODEL` / `PREFERENCE_LEARNING_MIN_SAMPLES` / `PREFERENCE_INJECT_TO_ROUTER`

**默认开关**:
- `PREFERENCE_LEARNING_ENABLED=true`(2026-04-16 切到 on,自动触发)
- `PREFERENCE_LEARNING_MIN_INTERVAL_HOURS=24`:两次自动推断之间的最短间隔
- `PREFERENCE_INJECT_TO_ROUTER=false`(推断偏好暂不自动注入 router prompt)

**启用后的新行为**:
- **自动触发点**:`MemoryManager.persist_job_outcome` 成功写入记忆(任务或 artifact)之后,调用 `maybe_run_learner(self)`
- **Gating**:状态文件 `data/preference_learn_state.json` 记录 `last_run_at`,距今 < `PREFERENCE_LEARNING_MIN_INTERVAL_HOURS` 直接跳过
- **容错**:推断/持久化失败不向外传播,只写日志;即便本次无候选也会更新 gate,避免频繁空跑
- **新增函数**:`should_run_now()` / `maybe_run_learner(manager)` —— 纯 I/O,不依赖 Chroma
- 手动触发仍然可用:`python main.py preference-learn [--dry-run]`

**测试**:`tests/test_preference_learner_unit.py` 升级到 15 条,新增 `TestShouldRunNow`(4 条)与 `TestMaybeRunLearner`(3 条):gate 节流 / 无历史时立即跑 / 正常持久化 / 无候选也写 gate / 关闭时不跑

**收口补充(2026-04-16)**:
- LLM 层已落地:`memory/preference_learner._distill_with_llm` + 新 prompt `prompts/preference_distillation.txt`
  - 规则层候选 + 原始统计 + 代表性任务记录 → 低成本模型输出额外候选(规则层权威;与规则层 key 相同时去重;`confidence < min_confidence` 丢弃)
  - LLM 候选 `source="llm_inferred"`,规则层 `source="inferred"`,`persist_inferred_preferences` 写入时 metadata 按原样保留
  - 触发条件:`PREFERENCE_LEARNING_MODEL` 非空时自动启用;失败吞掉,不影响规则层结果
- Router 注入:`core/router._build_inferred_preferences_block` 在 `analyze_intent` 中按 `PREFERENCE_INJECT_TO_ROUTER` 开关注入 "Inferred preferences" 区块
  - 从 `MEMORY_TIERED_ENABLED` 分层 → 读 `MEMORY_TIER_SEMANTIC_COLLECTION`;否则读 legacy
  - 只读 `source=inferred`、`confidence >= PREFERENCE_LEARNING_MIN_CONFIDENCE` 的行;最多 5 条;异常吞掉
- 默认开关仍为 `PREFERENCE_INJECT_TO_ROUTER=false` / `PREFERENCE_LEARNING_MODEL=""`,手动开启即可

**关闭方法**:`.env` 设置 `PREFERENCE_LEARNING_ENABLED=false` 停止自动触发;既有 `source=inferred` 偏好可通过 `/knowledge delete` 或直接按 metadata 过滤清理

---

## B. 网页操作优化

### B1. 站点选择器 + 登录流持久化(P3 落地)

#### B1.1 问题

- `agents/browser_execution.py:60` 的 `_element_cache` 只活在单次会话,下次打开同域名又要完整走感知+决策
- 登录、搜索等高频流程每次都"从零摸索",典型登录任务仍要 2-3 次 LLM(即使 P4 批量模式开启)
- `CLAUDE.md` 已预留 `BROWSER_PLAN_MEMORY_ENABLED`,但尚未实现

#### B1.2 设计

**三个资产**:

1. **站点选择器库**：`domain + element_role → successful_selectors(带命中次数和最近使用时间)`
2. **登录流模板**：`domain → LoginFlow{steps, auth_type, verification_hint}`
3. **导航模板**：常见操作(搜索、翻页、筛选)的参数化序列

**使用方式**:

- BrowserAgent 启动时按当前 URL 的 domain 查库
- 有登录流模板 → 直接跳过感知+决策,走"沿着模板执行 + 每步 DOM checkpoint 校验"路径
- 只有选择器库 → 注入到 `browser_act.txt` 的新变量 `{site_hints}`,LLM 优先采用但可覆盖
- 每次成功操作后,`record_success(domain, role, selector)`

#### B1.3 实现步骤

1. **数据层 `utils/site_knowledge_store.py`**
   - SQLite 存储(不用 Chroma,精确查询为主)
   - 表 `site_selectors(domain, role, selector, hit_count, last_used_at, success_rate)`
   - 表 `site_login_flows(domain, flow_json, last_success_at, auth_type)`
   - 表 `site_action_templates(domain, template_name, sequence_json, hit_count)`

2. **集成到执行层 `agents/browser_execution.py`**
   - 执行 click/input 前,优先查 `site_selectors`,命中则直接尝试
   - 失败降级到原 fallback 链
   - 成功后调 `record_success`,失败后 `record_failure`(用于清理坏 selector)

3. **集成到决策层 `agents/browser_decision.py`**
   - `_plan_next_action` 调用 `site_knowledge_store.get_hints(current_url)` 获取 top-K 选择器
   - 注入 `{site_hints}` 到 `browser_act.txt`(新增变量)

4. **登录流专用路径 `agents/browser_login_replay.py`**(新建)
   - `try_replay_login(agent, domain, credentials) -> LoginResult`
   - 成功返回 `success=True`,失败返回 `reason` 回退到常规流程
   - 每步使用 DOM checkpoint(复用 `utils/dom_checkpoint.py`)校验

5. **学习入口**:BrowserAgent 完成任务后
   - 若任务类型为 login/search 等,调 `site_knowledge_store.record_template(domain, template_name, executed_sequence)`

#### B1.4 新增配置

```bash
BROWSER_PLAN_MEMORY_ENABLED=false   # P3 主开关(原预留)
BROWSER_SITE_KNOWLEDGE_DB=data/site_knowledge.db
BROWSER_SELECTOR_HINT_TOP_K=5
BROWSER_LOGIN_REPLAY_ENABLED=true   # 只有主开关开启后才生效
BROWSER_SELECTOR_MIN_SUCCESS_RATE=0.6  # 低于此阈值的 selector 不再提示
BROWSER_SELECTOR_DECAY_DAYS=30      # 超过此天数未使用的 selector 降权
```

#### B1.5 测试

- `tests/test_site_knowledge_store_unit.py`
  - 插入/查询/成功失败计数准确
  - 低成功率 selector 不返回
- `tests/test_browser_login_replay_unit.py`
  - Mock 执行层,成功路径完整跑通
  - 某步骤 DOM checkpoint 失败 → 回退并标记 flow `last_failure_at`
- 集成测试:相同域名两次执行相同任务,第二次 LLM 调用次数显著下降(回归指标)

#### B1.6 回滚

- `BROWSER_PLAN_MEMORY_ENABLED=false` 完全跳过查询与写入
- 数据库文件可直接删除

---

### B2. 反爬 domain 画像 + 自适应节流

#### B2.1 问题

- `agents/web_worker.py:1837` 只在碰到 captcha / robot-check URL 时才反应
- `agents/browser_execution.py` 的 `detect_captcha` / `solve_captcha` 同样是"事后检测"
- 每次被拦截都是新一轮交互,不记录"这个域名倾向于拦截什么"

#### B2.2 设计

**domain 风险画像**:
- `domain → {block_rate, avg_delay_needed, preferred_ua, requires_headed, captcha_types_seen}`
- 首次请求前读画像 → 自适应延迟、UA 轮换、是否强制 headed 模式
- 被拦截后 `record_block(domain, kind)` 更新画像

#### B2.3 实现步骤

1. **新增 `utils/anti_bot_profile.py`**
   - SQLite(同 B1 共用 db 或独立均可)
   - `get_profile(domain) -> DomainProfile`
   - `record_request(domain, success: bool)`
   - `record_block(domain, kind: str)` — kind ∈ `{captcha, rate_limit, honeypot, unknown}`
   - `suggest_throttle(domain) -> ThrottleHint{delay_sec, ua, headed}`

2. **修改 `agents/web_worker.py` / `browser_agent.py`**
   - 新建 session 前读画像,应用 `ThrottleHint`
   - 每个请求前 sleep `delay_sec`(默认 0)
   - UA 从画像的 `preferred_ua` 取,否则按轮询池选

3. **反馈循环**
   - `agents/browser_execution.py` 的 `detect_captcha` 命中时 → `record_block(domain, "captcha")`
   - HTTP 429 / 503 → `record_block(domain, "rate_limit")`
   - 连续 N 次无拦截的成功 → 自动降低 `delay_sec`(收敛到零)

4. **UA 池**
   - `config/ua_pool.yaml`(新建),按平台分组

#### B2.4 新增配置

```bash
ANTI_BOT_PROFILE_ENABLED=true
ANTI_BOT_PROFILE_DB=data/anti_bot.db
ANTI_BOT_INITIAL_DELAY_SEC=0
ANTI_BOT_MAX_DELAY_SEC=5
ANTI_BOT_UA_POOL_FILE=config/ua_pool.yaml
ANTI_BOT_BLOCK_DECAY_DAYS=14   # 拦截事件的时间权重衰减
```

#### B2.5 测试

- `tests/test_anti_bot_profile_unit.py`
  - 连续 block 后 `suggest_throttle.delay_sec` 递增
  - 连续 success 后 `delay_sec` 衰减回零
  - `preferred_ua` 在多次成功后固化

#### B2.6 回滚

- `ANTI_BOT_PROFILE_ENABLED=false` 跳过全部逻辑
- 不干预原 captcha 检测路径

---

### B3. 视觉验证页面相似性缓存

#### B3.1 问题

- `agents/browser_perception.py:293` 每进入新 URL 就调视觉模型做描述
- 同类样板页面(搜索结果页、电商列表页)反复花视觉成本,但描述内容基本一致
- `_last_snapshot_hash` 只在单次动作循环内去重(line 64)

#### B3.2 设计

**页面相似性指纹**:
- `page_hash = hash(domain + route_template + dom_structural_signature)`
  - `route_template`:把 URL path 中的数字、hash、token 归一化为 `:id` / `:hash`
  - `dom_structural_signature`:取 top-level 标签结构 + 关键 landmark 角色(header/nav/main/footer)数量
- 持久化 `page_hash → vision_description, created_at, hit_count`
- 命中缓存时跳过视觉模型调用,直接用缓存描述

#### B3.3 实现步骤

1. **新增 `utils/page_fingerprint.py`**
   - `compute_page_hash(url: str, dom_summary: dict) -> str`
   - `normalize_url_path(url)` — 数字段 → `:id`,32+ 位 hex → `:hash`

2. **新增 `utils/vision_cache.py`**
   - SQLite 或 JSON 文件(小规模即可)
   - `get(page_hash) -> Optional[CachedVision]`
   - `set(page_hash, description, screenshot_path, ttl_days)`
   - TTL 由配置控制

3. **修改 `agents/browser_perception.py`**
   - 视觉调用前先算 `page_hash`,查缓存
   - 缓存未命中 → 走视觉 → 写缓存
   - 缓存命中 → 复用描述,同时重新抓一次 DOM(DOM 是必须的,只跳过视觉)

4. **安全兜底**:当任务包含高风险关键词(login、payment、verify)时强制跳过缓存,保证新鲜视觉描述

#### B3.4 新增配置

```bash
BROWSER_VISION_CACHE_ENABLED=true
BROWSER_VISION_CACHE_DB=data/vision_cache.db
BROWSER_VISION_CACHE_TTL_DAYS=7
BROWSER_VISION_CACHE_BYPASS_KEYWORDS=login,payment,checkout,verify,auth
```

#### B3.5 测试

- `tests/test_page_fingerprint_unit.py`
  - 相同模板不同 ID 的 URL 产生相同 hash
  - DOM 结构改变后 hash 变化
- `tests/test_vision_cache_unit.py`
  - 命中返回缓存,miss 调用视觉一次并写回
  - bypass 关键字触发跳缓存

#### B3.6 回滚

- `BROWSER_VISION_CACHE_ENABLED=false` 即完全禁用

---

### B4. iframe / 多 tab 真正支持

#### B4.1 问题

- `agents/browser_agent.py:51-56` 已定义 `SWITCH_IFRAME` / `EXIT_IFRAME` / `SWITCH_TAB` / `CLOSE_TAB` 枚举,但决策层从不输出这些动作
- `agents/browser_perception.py` 只看当前 frame 的 DOM,iframe 内元素完全"不可见"
- 典型场景:OAuth 登录(Google、GitHub 回调在 popup)、Stripe 支付(iframe)、客服 widget
- 目前行为:在主 frame 找不到元素 → 反复重试 → 超过 `BROWSER_STEP_STUCK_THRESHOLD` 卡死

#### B4.2 设计

**感知层扩展**:
- 每次 snapshot 时枚举所有 `frames` 和 `pages`(tabs)
- 为每个 frame/tab 提取"角色标签"(URL domain + 主要内容区关键词),便于 LLM 识别该去哪

**决策层扩展**:
- `browser_act.txt` prompt 加入"当前可用 frames / tabs"列表
- 当主 frame 找不到目标时,LLM 可输出 `SWITCH_IFRAME { frame_id: "..." }` 或 `SWITCH_TAB { index: N }`

**执行层扩展**:
- `agents/browser_execution.py` 实现 `switch_iframe` / `exit_iframe` / `switch_tab` / `close_tab`
- 维护当前上下文栈:`[page, frame1, frame2, ...]`
- 所有 element 查询接口(`find_by_role`、`find_by_text`)都相对当前栈顶作用

**启发式兜底**:
- 主 frame 连续 `STUCK_THRESHOLD` 次失败后,自动枚举所有可见 iframe,尝试在每个内查找目标
- 找到即切入,未找到继续回主 frame

#### B4.3 实现步骤

1. **`agents/browser_perception.py`**
   - 新方法 `enumerate_frames() -> List[FrameInfo]`
   - 新方法 `enumerate_tabs() -> List[TabInfo]`
   - snapshot payload 中加入 `frames` / `tabs` 数组

2. **`agents/browser_execution.py`**
   - 新增 `_context_stack: List[FrameHandle]`
   - 实现四个新动作方法
   - 查询函数均改为"对栈顶 frame 作用"

3. **`prompts/browser_act.txt`**
   - 新增 `{available_frames}` / `{available_tabs}` 变量
   - 描述条件:"若目标不在当前 frame,考虑 SWITCH_IFRAME"

4. **`agents/browser_decision.py`**
   - 解析 LLM 返回的 frame/tab 动作,委托到执行层
   - 增加"卡顿时启发式扫描 iframes"的逻辑分支

5. **新增测试页面 `tests/fixtures/iframe_login.html`**
   - 简单的主页面 + iframe 登录表单,端到端验证

#### B4.4 新增配置

```bash
BROWSER_IFRAME_ENABLED=true
BROWSER_TAB_MANAGEMENT_ENABLED=true
BROWSER_IFRAME_AUTO_SCAN_ON_STUCK=true   # 卡顿时自动扫 iframe
BROWSER_MAX_TAB_COUNT=10                  # 超过自动关闭最老的
```

#### B4.5 测试

- `tests/test_browser_iframe_unit.py`
  - 枚举 frames 返回结构正确
  - 切入后 `find_by_role` 只看 iframe 内元素
  - 切出后栈恢复
- `tests/test_browser_tab_unit.py`
  - 新开 tab 后切换上下文
  - 超过 `MAX_TAB_COUNT` 自动关闭最老
- 集成测试:用 fixture 页面完成 iframe 内登录

#### B4.6 回滚

- `BROWSER_IFRAME_ENABLED=false` + `BROWSER_TAB_MANAGEMENT_ENABLED=false` 回到单 frame 模式
- prompt 变量为空时 LLM 不会输出新动作,完全兼容

---

### B5. 失败策略自适应学习

#### B5.1 问题

- `agents/browser_execution.py:100-142` 的 fallback 链(CSS → text → role → label → force)写死顺序
- 同一 domain 上连续失败相同顺序,但系统不学习
- 某些站点 role-based 选择器永远失败,却仍在每次都先试

#### B5.2 设计

- 每次 click/input 记录 `{domain, element_role, strategy_tried, success, latency_ms}`
- 按 `(domain, role)` 聚合成功率,动态排序 fallback 策略
- 成功率低于阈值的策略直接跳过(省时间)
- 新 domain 冷启动时用全局平均排序

#### B5.3 实现步骤

1. **新增 `utils/strategy_stats.py`**
   - SQLite 或内存字典 + 周期落盘
   - `record(domain, role, strategy, success, latency)`
   - `ranked_strategies(domain, role) -> List[StrategyName]`(按成功率降序,未观测过的放中位数)
   - `skip_strategies(domain, role) -> Set[str]`(成功率 < 阈值的)

2. **修改 `agents/browser_execution.py`**
   - `_click_with_fallback(element_hint)` / `_input_with_fallback(...)` 内部从静态 list 改为 `strategy_stats.ranked_strategies(domain, role)`
   - 每次尝试结束调 `record(...)`

3. **埋点**
   - debug recorder 加 `strategy_attempt` 事件,便于分析

#### B5.4 新增配置

```bash
BROWSER_STRATEGY_LEARNING_ENABLED=true
BROWSER_STRATEGY_MIN_SAMPLES=5          # 低于此观测数不做排序,用全局默认
BROWSER_STRATEGY_SKIP_THRESHOLD=0.1     # 成功率低于此值跳过
BROWSER_STRATEGY_DB=data/browser_strategy.db
```

#### B5.5 测试

- `tests/test_strategy_stats_unit.py`
  - 样本数不足时返回默认顺序
  - 样本足够后按成功率排序
  - 低于阈值策略出现在 skip 集合
- 集成测试:模拟 20 次执行,验证后续 fallback 先选历史成功策略

#### B5.6 回滚

- `BROWSER_STRATEGY_LEARNING_ENABLED=false` 回到静态顺序
- 数据库可删除重建

---

### B6. BrowserAgent 三模式解耦重构

#### B6.1 问题

- `agents/browser_agent.py:2934-3206` 已有 legacy / unified / batch 三条分支
- 共享 perception / execution,但决策层的 mode 切换由多处 if/else 判定,维护成本走高
- 未来加入 B1/B4 后,模式组合更复杂

#### B6.2 设计

**策略模式重构**:

```
BrowserAgent
  └─ DecisionStrategy (abstract)
       ├─ LegacyPerStepStrategy
       ├─ UnifiedActStrategy      (P2)
       ├─ BatchExecuteStrategy    (P4)
       └─ LoginReplayStrategy     (B1,命中登录模板时)

  执行循环:
    loop:
      strategy = StrategyPicker.pick(state, config)
      result = strategy.decide_next(perception_snapshot)
      execute(result)
```

- `StrategyPicker.pick` 单点决策:"当前 URL 有登录模板 → LoginReplay;否则看 `BROWSER_BATCH_EXECUTE_ENABLED` > `BROWSER_UNIFIED_ACT_ENABLED` > legacy"
- 所有策略共享同一 `PageAssessmentCache`(按 `page_hash` 缓存当次感知结果),避免重复计算

#### B6.3 实现步骤

1. **新建 `agents/browser_strategies/` 包**
   - `base.py` — `DecisionStrategy` 抽象类(`decide_next`, `on_success`, `on_failure`)
   - `legacy.py` / `unified.py` / `batch.py` / `login_replay.py` — 逐一迁移现有代码

2. **`agents/browser_agent.py` 瘦身**
   - 执行循环只剩 `pick strategy → decide → execute → feedback`
   - 删除 inline 的 mode if/else

3. **`StrategyPicker`** 新模块,暴露 `pick(state, config) -> DecisionStrategy`

4. **共享缓存 `agents/page_assessment_cache.py`**
   - `get_or_compute(page_hash, compute_fn) -> PageAssessment`

5. **回归测试**
   - 现有 `tests/test_browser_batch_execute_unit.py` / `tests/test_browser_task_plan_unit.py` / `tests/test_browser_step_dedup_unit.py` 必须全绿
   - 新增 `tests/test_strategy_picker_unit.py`

#### B6.4 新增配置

无新增,沿用现有 `BROWSER_*_ENABLED` 开关,StrategyPicker 内部消化优先级。

#### B6.5 测试

- StrategyPicker 决策矩阵(组合开关)全部覆盖
- 每个 strategy 单测在 mock perception/execution 下能独立运行
- 集成回放:用历史 debug recorder 数据回放,对比重构前后结果一致

#### B6.6 回滚

- 保留原 `browser_agent.py` 为 `browser_agent_legacy.py`,通过 `BROWSER_STRATEGY_REFACTOR_ENABLED` 临时切换(重构稳定后删除)

---

## A′. 落地后的新增/修改文件清单(A 组 2026-04-16)

**新增(7 个)**:
- `memory/decay.py` — 衰减打分(A1)
- `memory/consolidator.py` — 老记忆归档(A1)
- `memory/entity_index.py` — 实体倒排索引(A2)
- `memory/tiered_store.py` — 分层存储(A4)
- `memory/preference_learner.py` — 偏好学习(A5)
- `prompts/memory_consolidation.txt` — 归档 prompt(A1)
- `scripts/migrate_memory_tiers.py` — 分层迁移脚本(A4)

**修改(4 个)**:
- `memory/scoped_chroma_store.py` — `search_memory` 衰减重排路径(A1)
- `memory/manager.py` — 实体索引接入、`search_by_entity` / `list_top_entities` / `persist_inferred_preferences`(A2/A5)
- `memory/skill_store.py` — `match_top_k` / `SkillMatch`(A3)
- `core/router.py` — `_build_skill_hint_block` 注入(A3)
- `main.py` — `memory-consolidate` / `preference-learn` CLI(A1/A5)
- `config/settings.py` — A1~A5 全部配置开关

**测试(6 个新文件,共 53 条)**:
- `tests/test_memory_decay_unit.py` 11 条
- `tests/test_memory_consolidator_unit.py` 8 条
- `tests/test_entity_index_unit.py` 7 条
- `tests/test_skill_hint_unit.py` 9 条
- `tests/test_tiered_memory_unit.py` 10 条
- `tests/test_preference_learner_unit.py` 8 条

**回归验证**:`tests/test_skill_store_unit.py` + `tests/test_router_unit.py` 全绿(共 67 条)。

---

## C. 落地与里程碑

### C.1 建议排期

| 阶段 | 内容 | 预估 |
|---|---|---|
| M1(第 1 周) | A1 衰减+TTL、A2 实体索引、A3 Skill 注入 | 纯记忆侧,互不冲突可并行 |
| M2(第 2 周) | B1 站点选择器+登录流、B5 失败策略学习 | 共用一个 SQLite,合并实现更省事 |
| M3(第 3 周) | B3 视觉缓存、B2 反爬画像 | 浏览器侧性能类优化 |
| M4(第 4 周) | B4 iframe/多 tab | 需要 fixture 页面+端到端测试 |
| M5(灰度后) | A4 记忆分层、A5 偏好学习、B6 三模式解耦 | 重构性质,稳定期后再动 |

### C.2 全局约束

- 所有新增配置遵循 `CLAUDE.md` 的 **No-Hardcoding Policy**:先进 `config/settings.py`,再由 `.env` 覆盖
- 所有新 prompt 进 `prompts/*.txt`,通过 `core/prompt_registry.py` 加载
- 所有新开关默认 `false` / 保守值,灰度一周后再切默认值
- 每个子项独立 PR,commit message 前缀 `feat(mem-A1)` / `feat(browser-B1)` 等

### C.3 指标埋点

统一通过 `utils/cost_tracker.py` 扩展:

- **记忆侧**:`memory_search_hit_rate`、`consolidation_run_count`、`entity_index_size`
- **浏览器侧**:`site_knowledge_hit_rate`、`vision_cache_hit_rate`、`strategy_skip_count`、`iframe_switch_count`
- 新增 `/memory-stats` 和 `/browser-stats` CLI 命令输出报告

### C.4 风险与缓解

| 风险 | 缓解 |
|---|---|
| 衰减/TTL 误删有用记忆 | `hit_count >= 1` 走摘要而非删除;保留 30 天软删除期 |
| 站点选择器缓存过期失效 | 失败率超阈值自动驱逐;记录 `last_success_at` 周期性复测 |
| iframe 无限递归 | 栈深度限制 + 每次切入记录 URL,同一 iframe 不重复进入 |
| 视觉缓存导致决策失误 | 高风险关键词(login/payment)强制 bypass;缓存 TTL 短 |
| 反爬画像过度保守 | `delay_sec` 指数衰减,连续 N 次成功立即归零 |

---

## D. 附录

### D.1 新增/修改文件清单

**新增**:
- `memory/decay.py` / `memory/consolidator.py` / `memory/entity_index.py` / `memory/preference_learner.py` / `memory/tiered_store.py`
- `utils/site_knowledge_store.py` / `utils/anti_bot_profile.py` / `utils/vision_cache.py` / `utils/page_fingerprint.py` / `utils/strategy_stats.py`
- `agents/browser_login_replay.py` / `agents/page_assessment_cache.py`
- `agents/browser_strategies/{base,legacy,unified,batch,login_replay}.py`
- `prompts/memory_consolidation.txt`
- `config/ua_pool.yaml`
- `scripts/migrate_memory_tiers.py`

**修改**:
- `memory/scoped_chroma_store.py`(衰减打分、hit_count 更新)
- `memory/manager.py`(实体索引接入、tiered 路由)
- `memory/skill_store.py`(`match_top_k`)
- `core/task_planner.py` / `core/router.py`(skill/偏好/实体注入)
- `agents/browser_agent.py`(策略模式瘦身)
- `agents/browser_perception.py`(frames/tabs 枚举、视觉缓存)
- `agents/browser_execution.py`(site hints、策略学习、iframe/tab 操作)
- `agents/browser_decision.py`(site hints prompt、iframe 启发式)
- `prompts/browser_act.txt`(新变量 `{site_hints}` / `{available_frames}` / `{available_tabs}`)
- `prompts/task_planner.txt`(新变量 `{related_skills}`)
- `config/settings.py`(所有新开关)
- `.env.example`(同步新配置)

### D.2 依赖关系图

```
A1(衰减) ──┬─→ A4(分层)
A2(实体) ──┘
A3(skill注入) ──→ 独立
A5(偏好学习) ──→ 依赖 A1 的 hit_count 更新

B1(站点知识) ──┬─→ B6(策略解耦)
B3(视觉缓存) ──┤
B5(策略学习) ──┤
B4(iframe) ────┘
B2(反爬画像) ──→ 独立
```
