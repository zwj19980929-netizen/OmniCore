# P0-2: Skill Library 自主学习与经验复用方案

> 优先级：P0 | 预估工作量：3-4 天 | 依赖：无

---

## 1. 目标

让 OmniCore 从每次成功执行的任务中自动提炼可复用的 Skill（技能），下次遇到类似任务时直接匹配执行，减少 LLM 规划开销、提升成功率。

**验收标准**：
- 成功完成的任务自动提炼为 Skill 存入 ChromaDB 专用 collection
- Router 阶段先检索 Skill Library，命中时跳过完整规划直接执行
- Skill 支持参数化模板、版本管理、失败自动降级
- 提供 `/skills` 命令查看和管理已有 Skill

---

## 2. 现有代码接入点

| 组件 | 文件 | 接入方式 |
|------|------|----------|
| ChromaDB 存储 | `memory/scoped_chroma_store.py` | 新增 `skill_definition` collection |
| 记忆管理 | `memory/manager.py` | 新增 Skill 提炼和检索方法 |
| 实体提取 | `memory/entity_extractor.py` | 复用现有实体提取能力 |
| Finalize 阶段 | `core/graph.py:1636-1730` | 在 finalize node 末尾触发 Skill 提炼 |
| Router 阶段 | `core/router.py:697-1013` | `analyze_intent()` 入口增加 Skill 检索 |
| 制品存储 | `utils/artifact_store.py` | Skill 关联制品引用 |
| 消息总线 | `core/message_bus.py` | 通过 MSG_TASK_RESULT 触发提炼 |
| 状态定义 | `core/state.py` | 在 `OmniCoreState` 中增加 `matched_skill` 字段 |

---

## 3. 数据模型设计

### 3.1 Skill 定义

```python
@dataclass
class SkillDefinition:
    """一个可复用的任务技能。"""
    skill_id: str                           # 唯一标识
    name: str                               # 技能名（如 "搜索商品价格"）
    description: str                        # 语义描述（用于向量检索匹配）
    version: int = 1                        # 版本号

    # 任务模板
    task_template: List[Dict[str, Any]] = field(default_factory=list)
    # 示例：
    # [
    #   {
    #     "tool_name": "browser.search_and_extract",
    #     "description_template": "在 {search_engine} 搜索 {query}",
    #     "params_template": {"url": "{search_url}", "task": "{query}"},
    #     "priority": 10,
    #   }
    # ]

    # 参数 schema
    parameters: Dict[str, Any] = field(default_factory=dict)
    # 示例：
    # {
    #   "query": {"type": "string", "description": "搜索关键词"},
    #   "search_engine": {"type": "string", "default": "google"},
    # }

    # 来源信息
    source_job_id: str = ""                 # 首次提炼的 Job ID
    source_intent: str = ""                 # 原始意图分类

    # 质量指标
    success_count: int = 0                  # 成功使用次数
    failure_count: int = 0                  # 失败使用次数
    last_used_at: str = ""                  # 最后使用时间
    deprecated: bool = False                # 是否已废弃

    # 元数据
    tags: List[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
```

### 3.2 ChromaDB Collection 设计

```python
# 在 ScopedChromaStore 中新增 collection
SKILL_COLLECTION = "omnicore_skills"

# 存储结构：
# document: Skill 的语义描述（用于向量检索）
# metadata:
#   skill_id: str
#   name: str
#   version: int
#   source_intent: str
#   success_count: int
#   failure_count: int
#   deprecated: str ("true"/"false")
#   tags: str (逗号分隔)
#   task_template_json: str (JSON 序列化)
#   parameters_json: str (JSON 序列化)
#   created_at: str
#   updated_at: str

# 检索方式：
# 1. 语义检索：用户输入 → 向量相似度 → top-K 候选
# 2. 过滤：deprecated=false, failure_count < threshold
# 3. 排序：success_count desc, 相似度 desc
```

---

## 4. 核心流程设计

### 4.1 Skill 提炼流程（Finalize 阶段）

```
任务完成
  ↓
[finalize node] → 检查任务是否成功
  ↓ (成功且 task_queue 有 >= 2 步)
[Skill 提炼 LLM] → 判断是否值得沉淀
  ↓ (值得)
[参数抽象] → 将具体值替换为参数模板
  ↓
[去重检查] → ChromaDB 语义检索，distance < 0.15 视为重复
  ↓ (非重复)
[写入 ChromaDB] → skill_definition collection
```

**Skill 提炼 Prompt**：

```python
SKILL_EXTRACTION_PROMPT = """
你是一个任务分析专家。请判断以下已成功完成的任务是否值得提炼为可复用技能。

## 判断标准
1. 任务至少包含 2 个步骤
2. 任务具有通用性（不是一次性的具体操作）
3. 任务流程可以参数化（具体值可以替换为变量）

## 已完成的任务
用户原始请求：{user_input}
意图分类：{intent}
执行步骤：
{task_queue_summary}

## 输出格式 (JSON)
{
    "worth_extracting": true/false,
    "reason": "简要说明原因",
    "skill_name": "技能名称",
    "skill_description": "技能的通用描述（用于未来匹配）",
    "parameters": {
        "param_name": {"type": "string", "description": "参数说明"}
    },
    "task_template": [
        {
            "tool_name": "原始 tool_name",
            "description_template": "包含 {param_name} 的描述模板",
            "params_template": {"key": "{param_name}"}
        }
    ],
    "tags": ["标签1", "标签2"]
}
"""
```

### 4.2 Skill 匹配流程（Router 阶段）

```
用户输入
  ↓
[Router.analyze_intent()] 入口
  ↓
[Skill 检索] → ChromaDB 语义检索 top-3
  ↓
过滤：distance < 0.3 && deprecated=false && failure_rate < 50%
  ↓
[匹配？]
  ├── 命中 → 参数填充 → 生成 task_queue → 跳过 LLM 规划
  └── 未命中 → 走正常 LLM 规划流程
```

**参数填充逻辑**：

```python
async def match_and_instantiate_skill(
    user_input: str,
    intent: str,
    shared_memory: Dict,
) -> Optional[List[Dict]]:
    """
    尝试匹配 Skill 并实例化为 task_queue。

    Returns:
        匹配成功返回 task_queue，否则返回 None
    """
    store = ScopedChromaStore()
    results = store.query(
        query=user_input,
        n_results=3,
        memory_type="skill_definition",
        where={"deprecated": "false"},
    )

    if not results or not results["documents"]:
        return None

    # 取最佳匹配
    best_distance = results["distances"][0][0]
    if best_distance > 0.3:
        return None

    best_metadata = results["metadatas"][0][0]
    skill_template = json.loads(best_metadata["task_template_json"])
    skill_params = json.loads(best_metadata["parameters_json"])

    # 使用 LLM 从用户输入中提取参数值
    param_values = await _extract_params_from_input(
        user_input, skill_params
    )

    if not param_values:
        return None

    # 实例化模板
    task_queue = []
    for i, step in enumerate(skill_template):
        task = {
            "task_id": f"skill_task_{i+1}",
            "tool_name": step["tool_name"],
            "description": step["description_template"].format(**param_values),
            "params": {
                k: v.format(**param_values) if isinstance(v, str) else v
                for k, v in step.get("params_template", {}).items()
            },
            "priority": step.get("priority", 10),
            "depends_on": [f"skill_task_{i}"] if i > 0 else [],
            "status": "pending",
            "skill_id": best_metadata["skill_id"],
        }
        task_queue.append(task)

    return task_queue
```

### 4.3 Skill 反馈更新

```python
async def update_skill_feedback(
    skill_id: str,
    success: bool,
) -> None:
    """任务完成后更新 Skill 的使用统计。"""
    store = ScopedChromaStore()

    # 获取当前 Skill
    result = store.get(ids=[skill_id], collection_name=SKILL_COLLECTION)
    if not result or not result["metadatas"]:
        return

    metadata = result["metadatas"][0]

    if success:
        metadata["success_count"] = str(int(metadata.get("success_count", "0")) + 1)
    else:
        metadata["failure_count"] = str(int(metadata.get("failure_count", "0")) + 1)

    metadata["updated_at"] = datetime.now().isoformat()

    # 自动降级：连续失败 >= 3 次且成功率 < 30%
    total = int(metadata["success_count"]) + int(metadata["failure_count"])
    if total >= 3:
        success_rate = int(metadata["success_count"]) / total
        if success_rate < 0.3:
            metadata["deprecated"] = "true"
            log_warning(f"Skill {skill_id} deprecated: success_rate={success_rate:.1%}")

    store.update(
        ids=[skill_id],
        metadatas=[metadata],
        collection_name=SKILL_COLLECTION,
    )
```

---

## 5. 代码修改清单

### 5.1 `memory/scoped_chroma_store.py`

新增 `skill_definition` collection 支持：

```python
# 在 MEMORY_TYPE_COLLECTIONS 映射中新增
MEMORY_TYPE_COLLECTIONS = {
    ...
    "skill_definition": "omnicore_skills",
}
```

### 5.2 `memory/skill_store.py`（新建）

```python
"""
Skill Library 存储层 — 提炼、检索、更新、管理 Skill。
"""

class SkillStore:
    def __init__(self):
        self._chroma = ScopedChromaStore()

    async def extract_and_save(self, state: OmniCoreState) -> Optional[str]:
        """从完成的任务中提炼 Skill。返回 skill_id 或 None。"""
        ...

    async def match(self, user_input: str, top_k: int = 3) -> Optional[SkillDefinition]:
        """语义匹配最佳 Skill。"""
        ...

    async def instantiate(self, skill: SkillDefinition, user_input: str) -> List[Dict]:
        """将 Skill 模板实例化为 task_queue。"""
        ...

    async def update_feedback(self, skill_id: str, success: bool) -> None:
        """更新 Skill 使用统计。"""
        ...

    def list_skills(self, include_deprecated: bool = False) -> List[SkillDefinition]:
        """列出所有 Skill。"""
        ...

    def deprecate_skill(self, skill_id: str) -> None:
        """手动废弃 Skill。"""
        ...

    def delete_skill(self, skill_id: str) -> None:
        """删除 Skill。"""
        ...
```

### 5.3 `core/graph.py` — finalize node 追加

在 `_finalize_node()` 末尾（约 line 1730）增加：

```python
# Skill 提炼（异步，不阻塞 finalize）
if _all_tasks_succeeded(state):
    try:
        from memory.skill_store import SkillStore
        skill_store = SkillStore()
        skill_id = await skill_store.extract_and_save(state)
        if skill_id:
            log_agent_action("SkillLibrary", f"Extracted skill: {skill_id}")
    except Exception as e:
        log_warning(f"Skill extraction failed (non-blocking): {e}")
```

### 5.4 `core/router.py` — analyze_intent 入口追加

在 `analyze_intent()` 方法的 LLM 调用之前（约 line 700）增加：

```python
# Skill Library 匹配（在 LLM 规划之前）
try:
    from memory.skill_store import SkillStore
    skill_store = SkillStore()
    matched_skill = await skill_store.match(user_input)
    if matched_skill:
        task_queue = await skill_store.instantiate(matched_skill, user_input)
        if task_queue:
            log_agent_action("Router", f"Skill matched: {matched_skill.name}")
            return {
                "intent": matched_skill.source_intent,
                "confidence": 0.9,
                "reasoning": f"Matched skill: {matched_skill.name}",
                "tasks": task_queue,
                "direct_answer": "",
                "is_high_risk": False,
                "skill_matched": True,
                "skill_id": matched_skill.skill_id,
            }
except Exception as e:
    log_warning(f"Skill matching failed (fallback to LLM): {e}")
```

### 5.5 `core/state.py` — 状态扩展

```python
# 在 OmniCoreState TypedDict 中新增
class OmniCoreState(TypedDict, total=False):
    ...
    matched_skill_id: str           # 匹配到的 Skill ID（用于反馈更新）
```

### 5.6 CLI 命令支持

在 `main.py` 中增加 `/skills` 快捷命令：

```python
elif user_input.startswith("/skills"):
    from memory.skill_store import SkillStore
    store = SkillStore()
    skills = store.list_skills()
    for s in skills:
        status = "🔴 deprecated" if s.deprecated else "🟢 active"
        rate = s.success_count / max(s.success_count + s.failure_count, 1)
        print(f"  {s.name} [{status}] 成功率={rate:.0%} 使用={s.success_count + s.failure_count}次")
```

---

## 6. 实施步骤

| 步骤 | 任务 | 产出 | 预估 |
|------|------|------|------|
| 1 | 定义 SkillDefinition 数据模型 | `memory/skill_store.py` 数据模型 | 1h |
| 2 | ChromaDB collection 注册 | `memory/scoped_chroma_store.py` 新增 mapping | 0.5h |
| 3 | 实现 Skill 提炼逻辑 | `SkillStore.extract_and_save()` + LLM prompt | 3h |
| 4 | 实现 Skill 匹配与实例化 | `SkillStore.match()` + `instantiate()` | 3h |
| 5 | 集成到 finalize node | `core/graph.py` 追加提炼调用 | 1h |
| 6 | 集成到 Router | `core/router.py` 追加匹配调用 | 1h |
| 7 | 实现反馈更新 | `SkillStore.update_feedback()` + 自动降级 | 1h |
| 8 | CLI `/skills` 命令 | `main.py` 快捷命令 | 0.5h |
| 9 | 编写测试 | `tests/test_skill_store_unit.py` | 2h |
| 10 | 端到端联调 | 执行任务 → 提炼 → 匹配 → 执行 | 2h |

---

## 7. 测试计划

```python
# tests/test_skill_store_unit.py

class TestSkillExtraction:
    async def test_extract_from_successful_task(self):
        """多步任务成功完成后应提炼出 Skill。"""

    async def test_skip_single_step_task(self):
        """单步任务不提炼。"""

    async def test_skip_failed_task(self):
        """失败任务不提炼。"""

    async def test_dedup_similar_skills(self):
        """语义相似的任务不重复提炼。"""


class TestSkillMatching:
    async def test_match_similar_input(self):
        """相似输入应命中已有 Skill。"""

    async def test_no_match_for_novel_input(self):
        """全新输入不应命中。"""

    async def test_deprecated_skill_not_matched(self):
        """已废弃的 Skill 不参与匹配。"""


class TestSkillInstantiation:
    async def test_params_correctly_filled(self):
        """参数应正确填入模板。"""

    async def test_task_queue_structure(self):
        """生成的 task_queue 结构应符合规范。"""


class TestSkillFeedback:
    async def test_success_increments(self):
        """成功使用应增加 success_count。"""

    async def test_auto_deprecation(self):
        """成功率 < 30% 且使用 >= 3 次应自动废弃。"""
```

---

## 8. 风险与注意事项

| 风险 | 缓解措施 |
|------|----------|
| 提炼质量不稳定 | 严格的 LLM prompt + 最少 2 步门槛 + 人工 `/skills` 管理 |
| 匹配误命中 | distance 阈值 0.3 + 参数提取失败时回退到 LLM 规划 |
| ChromaDB 性能 | Skill 数量一般不超过数百个，向量检索无压力 |
| 提炼 LLM 调用增加成本 | 只在成功任务的 finalize 阶段调用一次，使用低成本模型 |
| Skill 过时（工具变更后模板失效） | 自动降级机制 + `updated_at` 过期检查 |
