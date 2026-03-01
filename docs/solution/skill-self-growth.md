# Skill 自我成长机制 — 可行性分析与实现方案

## 1. 问题背景与动机

### 1.1 当前痛点

OmniCore 当前的任务执行流程是：用户输入 → Router 意图分析 → Worker 执行 → Validator/Critic 校验 → 输出结果。每次执行都是"从零开始"——即使用户反复执行相似任务（如"每天抓取 Hacker News 前 5 条"），系统仍然需要：

1. Router 重新做意图分析和任务拆解
2. Web Worker 重新让 LLM 分析页面结构、生成 CSS 选择器
3. Critic 重新审查相同模式的结果

这意味着：
- **重复的 LLM 调用开销**：相同任务每次都消耗 token
- **不稳定的执行质量**：LLM 每次生成的选择器可能不同，导致结果波动
- **无法积累经验**：系统不会因为"做过一次"而变得更快更准

### 1.2 目标愿景

引入 Skill 自我成长机制后，系统应具备：

- **技能沉淀**：成功执行的任务自动提炼为可复用的 Skill 文件
- **语义匹配**：新任务进来时，优先查找已有 Skill，命中则跳过 LLM 规划阶段
- **持续进化**：Skill 在多次执行中不断修正参数、更新选择器、提升成功率
- **自然淘汰**：长期失败或过时的 Skill 自动降权或归档

---

## 2. 核心思路

整个机制围绕四个阶段运转：**创建 → 匹配 → 复用 → 进化**。

```
用户输入
  ↓
┌─────────────────────────┐
│  Skill 匹配（Router 前） │ ← ChromaDB 语义搜索
└─────────┬───────────────┘
          │
    ┌─────┴─────┐
    │ 命中 Skill │──→ 直接注入 task_queue，跳过 Router 意图分析
    └─────┬─────┘
          │ 未命中
          ↓
    正常 Router 流程
          ↓
    Worker 执行 → Validator → Critic
          ↓
┌─────────────────────────────┐
│ Skill 创建/更新（Finalize 后）│ ← 从执行结果中提炼
└─────────────────────────────┘
```

### 2.1 创建：时空双重阈值驱动的经验结晶

技能不是"成功一次就沉淀"，而是"高频痛点的结晶"。系统引入**时空双重阈值 (Spatiotemporal Dual Threshold)** 来决定是否生成 Skill：

- **时间阈值 (Time)**：仅考察过去 7 天内的执行记录
- **空间阈值 (Space)**：Router 规划出的任务拓扑结构（Worker 序列 + 目标意图）高度相似，且成功执行了至少 3 次

只有同时满足两个阈值，才触发 Skill 提炼流程：

- 从 `OmniCoreState.task_queue` 提取任务编排模板
- 从 `shared_memory` 提取关键参数（URL、选择器、文件路径模式等）
- 从 PAOD trace 提取成功的执行路径
- 生成一份结构化的 Skill YAML 文件

这确保了一次性的复杂任务（如"抓取 A、B 两个不相关网站的数据对比后写个科幻故事"）不会污染技能库。

### 2.2 匹配：语义搜索 + 置信度阈值

新任务进入系统时，在 Router 之前插入一个 Skill 匹配步骤：

- 将用户输入 embedding 后在 ChromaDB 的 `skills` collection 中搜索
- 匹配结果超过置信度阈值（如 0.85）时，直接使用该 Skill
- 低于阈值则走正常 Router 流程

### 2.3 复用：参数化模板实例化

命中的 Skill 不是死板的脚本回放，而是参数化模板：

- 用 LLM 从用户输入中提取变量（如 URL、数量、文件名）
- 将变量填入 Skill 模板的 `params` 字段
- 生成完整的 `task_queue` 注入 State

### 2.4 进化：执行反馈驱动更新

每次 Skill 被复用后，根据执行结果更新元数据：

- 成功：`success_count += 1`，更新 `last_success_at`
- 失败：`fail_count += 1`，如果连续失败超过阈值，标记为 `degraded`
- 选择器变化：Web 类 Skill 检测到页面结构变化时，自动更新选择器

---

## 3. 技能文件格式设计

Skill 以 YAML 文件存储在 `data/skills/` 目录下，每个文件对应一个技能。

### 3.1 完整示例

```yaml
# data/skills/scrape-hackernews-top-n.yaml
skill_id: "scrape-hackernews-top-n"
version: 3
status: "active"  # active | degraded | archived

# --- 语义匹配区 ---
triggers:
  - "抓取 Hacker News 的热门新闻"
  - "获取 HN 排名前几的帖子"
  - "scrape top stories from Hacker News"
  - "帮我看看 Hacker News 今天有什么"
tags: ["web_scraping", "hacker_news", "news"]
negative_intents: ["reddit", "微博", "twitter"]  # 排他意图，用于多技能冲突时快速排除

# --- 参数接口（JSON Schema 强类型约束）---
interfaces:
  schema:
    type: "object"
    properties:
      url:
        type: "string"
        pattern: "^https?://[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}(/.*)?$"
        description: "目标网址"
        default: "https://news.ycombinator.com"
      limit:
        type: "integer"
        minimum: 1
        maximum: 99
        description: "抓取条数"
        default: 5
      output_path:
        type: "string"
        pattern: "^~/Desktop/[a-zA-Z0-9_\\-]+\\.[a-zA-Z0-9]+$"
        description: "输出文件路径，强制限定桌面目录"
        default: "~/Desktop/news_summary.txt"
    required: ["url", "limit"]
  extract_hints:
    limit: "从用户输入中提取数字，如'前10条'→10"
    output_path: "从用户输入中提取文件路径，未指定则用默认值"

# --- 任务编排模板 ---
task_template:
  - task_id: "task_1"
    task_type: "web_worker"
    description: "从 {url} 抓取前 {limit} 条新闻标题和链接"
    params:
      url: "{url}"
      limit: "{limit}"
      selectors:
        title: "tr.athing .titleline > a"
        link: "tr.athing .titleline > a::attr(href)"
    priority: 10
    success_criteria:
      - "len(result.get('data', [])) >= {limit}"
    fallbacks:
      - type: "retry"
        param_patch:
          headless: false

  - task_id: "task_2"
    task_type: "file_worker"
    description: "将抓取结果保存到 {output_path}"
    params:
      action: "write"
      path: "{output_path}"
      format: "txt"
    depends_on: ["task_1"]
    priority: 5

# --- 执行统计 ---
stats:
  created_at: "2026-02-15T10:30:00Z"
  last_success_at: "2026-02-28T14:22:00Z"
  last_fail_at: null
  success_count: 12
  fail_count: 1
  consecutive_failures: 0
  avg_execution_time_ms: 4500

# --- 来源追溯 ---
origin:
  first_user_input: "去 Hacker News 抓取排名前 5 的新闻标题和链接，保存到桌面"
  derived_from_session: "session_20260215_103000"
```

### 3.2 字段说明

| 字段 | 用途 |
|------|------|
| `triggers` | 多条自然语言描述，全部写入 ChromaDB 用于语义匹配 |
| `negative_intents` | 排他意图关键词，多技能冲突时用于快速排除不相关候选 |
| `interfaces.schema` | JSON Schema 强类型约束，通过 LLM Structured Output 从物理上隔绝注入攻击 |
| `interfaces.extract_hints` | 参数提取提示，辅助 LLM 从用户输入中定位变量 |
| `task_template` | 任务编排模板，变量用 `{param_name}` 占位 |
| `stats` | 运行时统计，驱动进化和淘汰逻辑 |
| `selectors` | Web 类 Skill 特有，缓存已验证的 CSS 选择器 |
| `origin` | 记录该 Skill 的诞生来源，便于调试 |

---

## 4. 技能匹配方案

### 4.1 存储结构

在现有 ChromaDB 实例（`memory/chroma_store.py`）中新增一个 `skills` collection：

```python
# memory/skill_store.py
class SkillStore:
    def __init__(self, chroma_client):
        self.collection = chroma_client.get_or_create_collection(
            name="skills",
            metadata={"hnsw:space": "cosine"}
        )

    def index_skill(self, skill: dict):
        """将 Skill 的所有 triggers 写入向量库"""
        for i, trigger in enumerate(skill["triggers"]):
            self.collection.upsert(
                ids=[f"{skill['skill_id']}__trigger_{i}"],
                documents=[trigger],
                metadatas=[{
                    "skill_id": skill["skill_id"],
                    "status": skill["status"],
                    "success_count": skill["stats"]["success_count"],
                    "fail_count": skill["stats"]["fail_count"],
                }]
            )

    def match(self, user_input: str, threshold: float = 0.85, n: int = 3):
        """语义匹配，返回置信度最高的 Skill"""
        results = self.collection.query(
            query_texts=[user_input],
            n_results=n,
            where={"status": {"$eq": "active"}}
        )
        if not results["distances"][0]:
            return None
        # ChromaDB cosine distance: 0=完全匹配, 2=完全相反
        # 转换为相似度: similarity = 1 - distance/2
        best_distance = results["distances"][0][0]
        similarity = 1 - best_distance / 2
        if similarity >= threshold:
            return {
                "skill_id": results["metadatas"][0][0]["skill_id"],
                "similarity": similarity,
                "matched_trigger": results["documents"][0][0],
            }
        return None
```

### 4.2 多触发描述策略

单条 trigger 容易漏匹配。每个 Skill 维护多条触发描述，覆盖不同表达方式：

- **创建时**：从用户原始输入生成 1 条，再让 LLM 补充 2-3 条同义变体
- **复用时**：如果用户输入命中了 Skill 但表述是新的，将该表述追加为新 trigger
- **上限控制**：每个 Skill 最多 10 条 trigger，超出时淘汰匹配频率最低的

### 4.3 置信度阈值设计

```
similarity >= 0.90  →  直接复用，不经过 Router
similarity >= 0.85  →  复用，但让 Router 做轻量校验（确认参数完整性）
similarity >= 0.70  →  作为 Router 的参考上下文注入 prompt，不直接复用
similarity <  0.70  →  忽略，走正常流程
```

阈值可在 `config/settings.py` 中配置：

```python
# Skill 匹配阈值
SKILL_MATCH_DIRECT_THRESHOLD = 0.90
SKILL_MATCH_ASSIST_THRESHOLD = 0.85
SKILL_MATCH_HINT_THRESHOLD = 0.70
```

---

## 5. 与现有架构的集成点

### 5.1 State 扩展

在 `core/state.py` 的 `OmniCoreState` 中新增字段：

```python
class OmniCoreState(TypedDict):
    # ... 现有字段保持不变 ...
    matched_skill: Dict[str, Any]       # 匹配到的 Skill 信息（skill_id, similarity）
    skill_params: Dict[str, Any]        # 从用户输入中提取的参数
    skill_execution_mode: str           # "skill_direct" | "skill_assist" | "skill_conflict" | "normal"
```

这三个字段均为可选，不影响现有流程——未命中 Skill 时它们为空，系统行为与当前完全一致。

### 5.2 Graph 集成：Router 前置匹配

在 `core/graph.py` 的图定义中，在 `route_node` 之前插入 `skill_match_node`：

```
START
  ↓
skill_match_node  ← 新增
  ├→ 唯一命中（≥0.90 且无冲突）→ param_extract_node → human_confirm_node → worker...
  ├→ 多技能冲突（差距 <0.05）  → clarification_node → 用户选择后回到 skill_match_node
  ├→ 辅助命中（≥0.85）        → route_node（注入 Skill 上下文）→ ...
  └→ 未命中                   → route_node（正常流程）→ ...
```

对应的条件路由函数：

```python
def after_skill_match(state: OmniCoreState) -> str:
    mode = state.get("skill_execution_mode", "normal")
    if mode == "skill_direct":
        return "param_extract"
    elif mode == "skill_conflict":
        return "clarification"
    else:
        return "route"  # skill_assist 和 normal 都走 Router
```

### 5.3 Graph 集成：Finalize 后置创建

在 `finalize_node` 之后，增加 `skill_learn_node`：

```
finalize_node
  ↓
skill_learn_node  ← 新增
  ↓
END
```

`skill_learn_node` 的逻辑（时空双重阈值）：

```python
def skill_learn_node(state: OmniCoreState) -> dict:
    user_input = state.get("user_input", "")
    task_queue = state.get("task_queue", [])

    # ── Step 0：无论成败，先将本次执行记录存入 session_history ──
    task_signature = _compute_task_signature(task_queue)
    session_store.record(
        user_input=user_input,
        task_signature=task_signature,
        success=state.get("critic_approved", False),
    )

    # ── Step 1：任务必须成功 ──
    if not state.get("critic_approved", False):
        return {}

    # ── Step 2：Skill 复用路径 → 只更新统计 / 触发复活 ──
    if state.get("skill_execution_mode") == "skill_direct":
        update_skill_stats(state["matched_skill"]["skill_id"], success=True)
        return {}

    # 如果本次是 degraded 技能退回 normal 后成功，触发复活
    degraded_skill_id = state.get("shared_memory", {}).get("degraded_origin_skill")
    if degraded_skill_id:
        resurrect_skill(degraded_skill_id, state)
        return {}

    # ── Step 3：时空双重阈值 — 拒绝一次性任务沉淀 ──
    similar_sessions = session_store.query_similar(
        user_input=user_input,
        threshold=0.85,
        days=7,
    )
    successful_matches = [
        s for s in similar_sessions
        if s["success"] and s["task_signature"] == task_signature
    ]

    if len(successful_matches) < 3:
        # 高频痛点尚未形成，拒绝生成技能
        return {}

    # ── Step 4：达到阈值，泛化提炼 Skill ──
    # 将多次成功执行的历史 user_input 一并传给 LLM，
    # 让它归纳出通用的执行模式，而非某次具体实例
    history_inputs = [s.get("user_input", "") for s in successful_matches]
    skill = extract_generalized_skill(state, history_inputs)
    save_skill(skill)
    skill_store.index_skill(skill)
    return {}
```

`extract_generalized_skill` 是技能泛化提炼的核心——它不是从单次执行中"复制粘贴"，而是从多次同模式执行中"归纳抽象"：

```python
def extract_generalized_skill(state: OmniCoreState, history_inputs: list[str]) -> dict:
    """从多次同模式的成功执行中，归纳出泛化的 Skill 模板。

    核心原则：提炼的是"执行模式"，不是"具体实例"。
    例如从以下 3 次执行中：
      - "对比京东和亚马逊 iPhone 16 价格"
      - "对比淘宝和京东 iPhone 17 价格"
      - "对比淘宝和亚马逊 iPhone 16 价格"
    归纳出的 Skill 应该是"多站商品价格对比"，参数为 site_a, site_b, product。
    """
    task_queue = state.get("task_queue", [])

    # 让 LLM 从多次历史输入中归纳出：
    # 1. 泛化的技能名称和描述（不含具体站点/商品名）
    # 2. 参数化变量列表（从具体值中抽象出变量）
    # 3. 泛化的 triggers（覆盖这类任务的通用表述）
    generalization = llm.chat(
        messages=[{
            "role": "user",
            "content": (
                "以下是用户多次执行的同类任务：\n"
                f"{json.dumps(history_inputs, ensure_ascii=False)}\n\n"
                f"本次执行的任务编排：\n"
                f"{json.dumps([{'task_type': t['task_type'], 'description': t['description']} for t in task_queue], ensure_ascii=False)}\n\n"
                "请归纳出一个泛化的技能模板：\n"
                "1. skill_name: 简短的技能名（不含具体站点/商品/人名等实例）\n"
                "2. params: 从这些具体任务中抽象出的变量列表，每个变量包含 name, type, description\n"
                "3. triggers: 3-4 条泛化的自然语言描述，覆盖这类任务的通用表述\n"
                "4. task_descriptions: 用变量占位符重写每个 task 的 description\n"
                "返回 JSON。"
            )
        }],
        json_mode=True,
    )

    # 用 LLM 归纳结果 + 本次执行的拓扑结构，组装 Skill YAML
    skill = {
        "skill_id": generalization["skill_name"],
        "version": 1,
        "status": "active",
        "triggers": generalization["triggers"],
        "tags": _infer_tags(task_queue),
        "negative_intents": [],
        "interfaces": {
            "schema": _build_schema_from_params(generalization["params"]),
            "extract_hints": {p["name"]: p["description"] for p in generalization["params"]},
        },
        "task_template": _build_generalized_template(task_queue, generalization),
        "stats": _init_stats(),
        "origin": {
            "first_user_input": state.get("user_input", ""),
            "derived_from_inputs": history_inputs,
        },
    }
    return skill
```

这样生成的 Skill 天然是泛化的——"多站商品价格对比"而非"京东亚马逊 iPhone 16 对比"，参数是 `{site_a}`, `{site_b}`, `{product}` 而非硬编码的具体值。

其中 `session_store` 是 ChromaDB 中新增的 `session_history` collection：

```python
# memory/skill_store.py — SessionHistoryStore
class SessionHistoryStore:
    def __init__(self, chroma_client):
        self.collection = chroma_client.get_or_create_collection(
            name="session_history",
            metadata={"hnsw:space": "cosine"}
        )

    def record(self, user_input: str, task_signature: str, success: bool):
        """记录每次任务执行的摘要"""
        session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
        self.collection.add(
            ids=[session_id],
            documents=[user_input],
            metadatas=[{
                "task_signature": task_signature,
                "success": success,
                "user_input": user_input,  # 冗余存储，供泛化提炼时回溯历史表述
                "timestamp": datetime.now().isoformat(),
            }]
        )

    def query_similar(self, user_input: str, threshold: float, days: int) -> list:
        """查询指定天数内与 user_input 语义相似的历史记录"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        results = self.collection.query(
            query_texts=[user_input],
            n_results=20,
            where={"timestamp": {"$gte": cutoff}},
        )
        out = []
        for i, dist in enumerate(results["distances"][0]):
            similarity = 1 - dist / 2
            if similarity >= threshold:
                meta = results["metadatas"][0][i]
                out.append({
                    "similarity": similarity,
                    "success": meta["success"],
                    "task_signature": meta["task_signature"],
                    "user_input": meta.get("user_input", ""),
                })
        return out


def _compute_task_signature(task_queue: list) -> str:
    """将任务拓扑骨架哈希为指纹，只关心 Worker 类型的有序序列。

    抽象到最高层：只看"用了哪些类型的 Worker、按什么顺序排列"。
    例如：
      - 对比京东和亚马逊 iPhone 16  → "web_worker→web_worker→file_worker"
      - 对比淘宝和京东 iPhone 17    → "web_worker→web_worker→file_worker"
      - 抓取 HN 保存到桌面          → "web_worker→file_worker"
    前两者签名相同，第三个不同。
    """
    type_sequence = "→".join(t.get("task_type", "unknown") for t in task_queue)
    return hashlib.md5(type_sequence.encode()).hexdigest()
```

### 5.4 参数提取节点（JSON Schema Structured Output + 断路器）

当 Skill 直接命中时，通过 LLM 的 Structured Output 能力直接约束输出格式，从物理上隔绝 Prompt Injection 和路径穿越攻击。不再使用"先生成，后正则拦截"的后置校验模式。

```python
from jsonschema import validate, ValidationError

def param_extract_node(state: OmniCoreState) -> dict:
    skill = load_skill(state["matched_skill"]["skill_id"])
    user_input = state["user_input"]
    schema = skill["interfaces"]["schema"]
    hints = skill["interfaces"].get("extract_hints", {})

    # 构造提取 prompt，将 JSON Schema 直接传给 LLM 的 response_format
    hint_text = "\n".join(f"  {k}: {v}" for k, v in hints.items())
    extracted = llm.chat(
        messages=[{
            "role": "user",
            "content": f"从以下用户输入中提取参数，缺失的参数用 null 表示。\n"
                       f"用户输入：{user_input}\n"
                       f"提取提示：\n{hint_text}"
        }],
        response_format={"type": "json_schema", "json_schema": schema},
    )

    # 填充默认值
    properties = schema.get("properties", {})
    for key, prop in properties.items():
        if extracted.get(key) is None and "default" in prop:
            extracted[key] = prop["default"]

    # ── 断路器：JSON Schema 硬校验 ──
    try:
        validate(instance=extracted, schema=schema)
    except ValidationError as e:
        # 校验失败 → 直接熔断，绝不带病执行
        return {
            "execution_status": "error",
            "error_trace": f"参数安全校验未通过: {e.message}",
            "final_output": f"参数安全校验未通过，已终止执行。\n原因：{e.message}",
        }

    # 校验通过，实例化模板
    task_queue = instantiate_template(skill["task_template"], extracted)

    return {
        "skill_params": extracted,
        "task_queue": task_queue,
        "current_intent": skill["tags"][0] if skill.get("tags") else "multi_step_task",
    }
```

与旧方案（regex 后置拦截）的关键区别：

| 维度 | 旧方案（regex） | 新方案（JSON Schema） |
|------|-----------------|----------------------|
| 校验时机 | LLM 输出后用 Python re 拦截 | LLM 输出时即受 schema 约束 |
| 攻击面 | LLM 可能输出任意字符串再拦截 | 从物理上限制输出结构 |
| 失败处理 | 回退默认值，静默继续 | 断路器熔断，直接终止 |
| 类型安全 | 仅字符串匹配 | 支持 integer/string/enum/pattern 等 |

### 5.5 集成总览图

```
START
  ↓
skill_match_node ─── 唯一命中 ──→ param_extract_node ─────┐
  │                                                         │
  │── 多技能冲突 ──→ clarification_node ──→ skill_match_node│
  │                  （挂起，主动反问用户）                    │
  │                                                         │
  │── 辅助/未命中 ──→ route_node                             │
  │                      ↓                                  │
  │                 human_confirm_node ←───────────────────┘
  │                      ↓
  │              [web/file/system/browser worker]
  │                      ↓
  │                 validator_node
  │                   ↓      ↓
  │              critic    replanner
  │                ↓
  │           finalize_node
  │                ↓
  └──────→  skill_learn_node  ← 成功时提炼/更新 Skill
                   ↓
                  END
```

---

## 6. 边界情况处理

### 6.1 技能过时（选择器失效）

Web 类 Skill 最容易过时——目标网站改版后，缓存的 CSS 选择器会失效。

**检测方式**：Validator 发现 `data` 为空或长度不符合 `success_criteria` 时，`consecutive_failures` 递增。

**处理策略**：选择器失效属于连续失败的一种，由 6.4 节的"断路退回与浴火重生"机制统一处理：

```
Skill 复用 → Worker 执行 → Validator 失败
  ↓
consecutive_failures += 1
  ↓
达到 5 次 → 状态变为 degraded
  ↓
下次匹配时触发断路退回 → Router 重新分析页面结构
  ↓
探索成功 → resurrect_skill() 用新选择器复活 Skill
```

这比原来的"单次失败即尝试更新选择器"更稳健——避免了因网站临时故障导致的误更新。

### 6.2 多技能冲突

用户输入可能同时匹配多个 Skill，例如"抓取新闻"可能匹配到 HackerNews Skill 和 Reddit Skill。

**处理策略：拦截挂起 + 主动反问，绝不盲猜**

核心原则：当多个 Skill 置信度均 >0.85 且相差 < 0.05 时，系统必须中止执行，跳转至 `clarification_node` 向用户主动反问，绝不自动替用户做主。

1. **唯一胜出**：最高相似度的 Skill 与第二名差距 ≥ 0.05 → 直接使用最高的
2. **拦截挂起**：差距 < 0.05 → 中止执行，图状态跳转至 `clarification_node`，向终端输出选项让用户确认
3. **排他意图辅助**：Skill YAML 中可选增加 `negative_intents` 字段（如财务报表 Skill 标注 `negative_intents: ["运营", "销售"]`），匹配时用于快速排除明显不相关的候选

Graph 中新增 `clarification_node`：

```
skill_match_node
  ├→ 唯一命中        → param_extract_node
  ├→ 多技能冲突      → clarification_node → skill_match_node（用户选择后重新匹配）
  └→ 未命中          → route_node
```

```python
def select_best_skill(candidates: list) -> dict | None:
    """从多个候选 Skill 中选择最优，无法确定时返回 None 触发反问"""
    if len(candidates) == 1:
        return candidates[0]

    # 按相似度降序
    candidates.sort(key=lambda x: x["similarity"], reverse=True)

    top = candidates[0]
    runner_up = candidates[1]

    # 先用 negative_intents 排除明显不相关的
    filtered = []
    for c in candidates:
        skill = load_skill(c["skill_id"])
        negatives = skill.get("negative_intents", [])
        # 如果用户输入命中了该 Skill 的排他意图，排除之
        if not any(neg in user_input for neg in negatives):
            filtered.append(c)

    if len(filtered) == 1:
        return filtered[0]
    if not filtered:
        return candidates[0]  # 全被排除则回退到最高相似度

    # 排除后仍有多个，检查差距
    filtered.sort(key=lambda x: x["similarity"], reverse=True)
    if filtered[0]["similarity"] - filtered[1]["similarity"] >= 0.05:
        return filtered[0]

    # 差距过小，拒绝盲猜，返回 None 触发 clarification_node
    return None


def clarification_node(state: OmniCoreState) -> dict:
    """多技能冲突时，挂起执行并向用户主动反问"""
    candidates = state["matched_skill"]["candidates"]
    options = []
    for c in candidates:
        skill = load_skill(c["skill_id"])
        options.append(f"  [{c['skill_id']}] {skill['triggers'][0]} (相似度: {c['similarity']:.2f})")

    prompt = "检测到多个匹配的技能，请确认您的意图：\n" + "\n".join(options)

    return {
        "execution_status": "waiting_for_clarification",
        "needs_human_confirm": True,
        "final_output": prompt,
    }
```

### 6.3 参数提取失败

参数提取可能在两个阶段失败：LLM 未能提取到必需参数、或提取结果未通过 JSON Schema 校验。

**处理策略：分级降级 + 断路器熔断**

1. **必需参数缺失（LLM 返回 null）**：检查 `schema.required` 中的字段，如果有默认值则填充；如果无默认值，降级为 `skill_assist` 模式，将 Skill 模板作为参考上下文传给 Router 做完整意图分析
2. **JSON Schema 校验失败（ValidationError）**：断路器熔断，直接终止图流转，将状态置为 `error`，告知用户"参数安全校验未通过"。绝不允许带病执行
3. 记录降级/熔断事件，用于后续优化 `extract_hints`

```python
def handle_param_extraction(extracted: dict, schema: dict) -> tuple[str, dict]:
    """
    返回 (action, payload)
    action: "proceed" | "degrade" | "circuit_break"
    """
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    # 填充默认值
    for key, prop in properties.items():
        if extracted.get(key) is None and "default" in prop:
            extracted[key] = prop["default"]

    # 检查必需参数是否齐全
    missing_required = [k for k in required if extracted.get(k) is None]
    if missing_required:
        return "degrade", {"missing": missing_required}

    # JSON Schema 硬校验
    try:
        validate(instance=extracted, schema=schema)
    except ValidationError as e:
        return "circuit_break", {"error": e.message, "path": list(e.absolute_path)}

    return "proceed", {"params": extracted}
```

### 6.4 技能降权：断路退回与浴火重生

原始设计中，`degraded` 状态的技能存在逻辑死锁：如果匹配时被过滤掉，它永远无法积累 `success_count`，等同于"假死"；如果不被过滤，用户会遭遇重复失败。

**解决方案：断路退回 + 自动复活**

#### 断路退回（在 `skill_match_node` 中）

匹配到 `degraded` 技能时，不执行也不忽略，而是强制退回 normal 模式重新探索：

```python
def skill_match_node(state: OmniCoreState) -> dict:
    user_input = state["user_input"]
    match_result = skill_store.match(
        user_input,
        threshold=SKILL_MATCH_HINT_THRESHOLD,
        include_degraded=True,  # 不过滤 degraded，而是特殊处理
    )

    if match_result is None:
        return {"skill_execution_mode": "normal"}

    skill = load_skill(match_result["skill_id"])

    # ── 断路器：degraded 技能强制退回探索模式 ──
    if skill["status"] == "degraded":
        log_warning(
            f"技能 [{skill['skill_id']}] 处于降权状态，"
            f"退回慢速探索模式重新执行。"
        )
        return {
            "skill_execution_mode": "normal",  # 强制走 Router
            "shared_memory": {
                **state.get("shared_memory", {}),
                "degraded_origin_skill": skill["skill_id"],  # 标记来源，用于复活
            },
            "final_output": "检测到目标系统的结构可能已发生重大变更，"
                           "原有技能失效。本次操作将回退为慢速探索模式重新执行。",
        }

    # 正常匹配逻辑（active 技能）...
    if match_result["similarity"] >= SKILL_MATCH_DIRECT_THRESHOLD:
        return {"skill_execution_mode": "skill_direct", "matched_skill": match_result}
    elif match_result["similarity"] >= SKILL_MATCH_ASSIST_THRESHOLD:
        return {"skill_execution_mode": "skill_assist", "matched_skill": match_result}
    else:
        return {"skill_execution_mode": "normal"}
```

#### 浴火重生（在 `skill_learn_node` 中）

当退回 normal 模式的任务成功走通后，用新的执行结果复活对应的 degraded 技能：

```python
def resurrect_skill(skill_id: str, state: OmniCoreState):
    """用本次成功的执行结果全量重生 degraded 技能

    注意：不做旧模板的局部修补。退回 normal 模式后 Router 可能规划出
    完全不同的任务拓扑，因此直接用本次跑通的 task_queue
    全量提炼新的 task_template，彻底替换旧模板。
    保留旧 Skill 的 triggers 和 interfaces 框架（它们描述的是用户意图，
    不会因为网站改版而失效），只替换执行层的 task_template。
    """
    skill = load_skill(skill_id)

    # 全量重生：从本次成功的 state 中提炼全新的 task_template
    new_template = extract_task_template_from_state(state)

    skill["task_template"] = new_template
    skill["status"] = "active"
    skill["version"] = skill.get("version", 1) + 1
    skill["stats"]["consecutive_failures"] = 0
    skill["stats"]["last_success_at"] = datetime.now().isoformat()

    save_skill(skill)
    skill_store.index_skill(skill)  # 重新写入 ChromaDB
    log_success(f"技能 [{skill_id}] 已全量重生 → v{skill['version']}")
```

#### 完整生命周期状态机

```
                  创建成功
                    ↓
    ┌──────→  [ active ] ←──── 浴火重生（复活）
    │              │
    │    consecutive_failures >= 5
    │              ↓
    │        [ degraded ] ──→ 退回 normal 模式探索
    │              │                    │
    │              │              探索成功？
    │              │              ├→ 是 → resurrect_skill() → active
    │              │              └→ 否 → 保持 degraded
    │              │
    │    degraded 持续 30 天无成功
    │              ↓
    │        [ archived ] ──→ 从 ChromaDB 移除
    │
    │  success_count==0 且创建超 7 天
    └──────────────────────────────────┘
```

#### 淘汰规则

| 条件 | 动作 |
|------|------|
| `consecutive_failures >= 5` | 状态改为 `degraded`，后续匹配时触发断路退回 |
| `degraded` + 退回探索成功 | 复活为 `active`，更新选择器和参数，version +1 |
| `degraded` 持续 30 天无复活 | 状态改为 `archived`，从 ChromaDB 中移除 |
| `success_count == 0` 且创建超过 7 天 | 直接 `archived`（从未成功过的废弃 Skill） |
| 手动标记 | 用户可通过命令 `skill archive <id>` 手动归档 |

```python
def check_skill_health(skill: dict) -> str:
    """定期健康检查，返回建议状态"""
    stats = skill["stats"]

    if stats["consecutive_failures"] >= 5 and skill["status"] == "active":
        return "degraded"

    if skill["status"] == "degraded":
        last_success = parse_datetime(stats.get("last_success_at"))
        if not last_success or (now() - last_success).days > 30:
            return "archived"

    if stats["success_count"] == 0:
        created = parse_datetime(stats["created_at"])
        if (now() - created).days > 7:
            return "archived"

    return skill["status"]  # 保持当前状态
```

### 6.5 安全约束

Skill 复用不能绕过安全机制：

- **human_in_the_loop 保持生效**：即使 Skill 直接命中，高危操作仍需人类确认。`human_confirm_node` 在 `param_extract_node` 之后，不会被跳过。
- **Skill 文件不包含凭证**：参数模板中不存储密码、API Key 等敏感信息。
- **Skill 来源可追溯**：`origin` 字段记录创建来源，便于审计。

---

## 7. 实现路径建议

### 7.1 分阶段落地

**Phase 1 — 基础骨架（最小可用）**

- 实现 `SkillStore` + `SessionHistoryStore`（ChromaDB skills / session_history collection）
- 实现 `skill_match_node`（匹配 + 阈值判断 + degraded 断路退回）
- 实现 `skill_learn_node`（时空双重阈值 + 复活机制）
- 手动创建 2-3 个种子 Skill 验证流程

**Phase 2 — 参数化复用**

- 实现 `param_extract_node`（JSON Schema Structured Output + 断路器熔断）
- 实现模板实例化逻辑
- 接入 Graph 条件路由 + `clarification_node`

**Phase 3 — 进化与淘汰**

- 实现统计更新逻辑
- 实现 degraded → 断路退回 → 复活 完整链路
- 实现健康检查与自动归档
- 实现 trigger 自动扩充

### 7.2 新增文件清单

```
core/
  skill_matcher.py       # skill_match_node + param_extract_node + clarification_node + degraded 断路
  skill_learner.py       # skill_learn_node + 时空双重阈值 + resurrect_skill
memory/
  skill_store.py         # SkillStore + SessionHistoryStore (ChromaDB collections)
data/
  skills/                # Skill YAML 文件存储目录
    *.yaml
config/
  settings.py            # 新增 SKILL_MATCH_*_THRESHOLD 配置项
```

### 7.3 对现有代码的改动范围

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `core/state.py` | 新增字段 | 添加 `matched_skill`、`skill_params`、`skill_execution_mode` |
| `core/graph.py` | 新增节点 + 边 | 插入 `skill_match_node`、`param_extract_node`、`clarification_node`、`skill_learn_node` |
| `config/settings.py` | 新增配置 | 添加匹配阈值、Skill 目录路径等 |
| `memory/chroma_store.py` | 无改动 | 复用现有 ChromaDB client，新建 collection 即可 |
| `core/router.py` | 微调 | `skill_assist` 模式下在 prompt 中注入 Skill 上下文 |

其余模块（Worker、Validator、Critic、PAOD）无需改动。

---

## 8. 可行性结论

**结论：可行，且与现有架构高度兼容。**

核心依据：

1. **基础设施已就绪**：ChromaDB 已集成（`memory/chroma_store.py`），新增 `skills` + `session_history` 两个 collection 即可支持技能语义搜索和执行历史追踪，无需引入新存储依赖。`jsonschema` 为轻量级纯 Python 库，零外部依赖。

2. **架构天然支持扩展**：LangGraph 的节点式编排允许在任意位置插入新节点。`skill_match_node` 插在 Router 前、`skill_learn_node` 插在 Finalize 后，不破坏现有流程。

3. **State 设计兼容**：`OmniCoreState` 使用 `TypedDict`，新增可选字段不影响现有节点——未命中 Skill 时这些字段为空，所有现有逻辑照常运行。

4. **渐进式上线**：Phase 1 只需 3 个新文件 + 3 处微调，可以在不影响现有功能的前提下验证核心假设。

5. **成本收益明确**：每次 Skill 命中可节省 Router 意图分析 + Worker 页面结构分析的 LLM 调用（约 2-3 次 API call），对高频重复任务的提速效果显著。

6. **三重防御机制到位**：
   - **时空双重阈值**防止技能库被一次性任务污染，确保只有高频痛点才结晶为 Skill
   - **JSON Schema Structured Output + 断路器**从物理上隔绝参数注入攻击，校验失败直接熔断
   - **断路退回 + 浴火重生**解决 degraded 技能的逻辑死锁，让失效技能有机会自我修复

**主要风险**：

- Skill 匹配的误命中（用户意图相似但目标不同）→ 通过高阈值 + clarification_node 主动反问缓解
- Web 选择器过时导致连续失败 → 通过 degraded 断路退回 + 自动复活缓解
- session_history 数据膨胀 → 定期清理超过 30 天的历史记录
- LLM 不支持 Structured Output → 回退到 json_mode + jsonschema 后置校验（断路器仍生效）
