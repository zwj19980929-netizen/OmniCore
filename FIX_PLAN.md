# 网页感知问题修复方案

## 🎯 修复目标

解决 Agent 在网页中"迷路"的四个核心问题：
1. 只看交互元素，看不到内容
2. 暴力截断 HTML
3. Token 噪音过多
4. 缺少"理解-决策"流程

## 📊 当前状态评估

| 问题 | 状态 | 已有工具 | 缺失环节 |
|------|------|---------|---------|
| 1. 内容盲区 | 🟡 60% | PagePerceiver 已实现 | 未集成到主流程 |
| 2. 暴力截断 | 🟢 90% | 已改为 100k + 清洗 | 基本解决 |
| 3. Token 噪音 | 🟢 95% | _clean_html_for_llm 已生效 | 基本解决 |
| 4. 缺少理解 | 🟡 40% | EnhancedWebWorker 已实现 | 未被主流程调用 |

## 🔧 修复方案（分3阶段）

---

## 阶段1：修复 web_worker 的页面感知（优先级：🔴 最高）

### 目标
让 `web_worker.py` 在分析页面时能看到完整的页面结构。

### 修改文件
- `agents/web_worker.py`
- `prompts/page_analysis.txt`

### 具体步骤

#### Step 1.1: 修改 `analyze_page_structure` 方法

**位置：** `agents/web_worker.py:1393-1459`

**修改内容：**

```python
async def analyze_page_structure(self, tk: BrowserToolkit, task_description: str) -> Dict[str, Any]:
    log_agent_action(self.name, "分析页面结构")
    url_r = await tk.get_current_url()
    html_r = await tk.get_page_html()
    html = html_r.data or ""

    # 先清洗HTML（移除script/style/注释/空白）
    html = RE_SCRIPT_TAG.sub('', html)
    html = RE_STYLE_TAG.sub('', html)
    html = RE_HTML_COMMENT.sub('', html)
    html = RE_WHITESPACE.sub(' ', html)

    # 🔥 新增：获取页面结构化描述
    from utils.page_perceiver import PagePerceiver
    perceiver = PagePerceiver()
    try:
        page_structure = await perceiver.perceive_page(tk, task_description)
        page_structure_text = page_structure.to_llm_prompt()
        log_agent_action(self.name, "页面结构提取完成", f"{len(page_structure.main_content_blocks)} 个内容块")
    except Exception as e:
        log_warning(f"页面结构提取失败，降级为纯HTML分析: {e}")
        page_structure_text = "(页面结构提取失败，仅使用HTML分析)"

    normalized_url = self.cache.normalize_url(url_r.data or "")
    task_signature = self.cache.build_task_signature(task_description)
    page_fingerprint = self.cache.build_page_fingerprint(html)
    cache_key = self.cache.build_key(
        "page_structure_analysis",
        normalized_url=normalized_url,
        task_signature=task_signature,
        page_fingerprint=page_fingerprint,
        prompt_version="page_analysis_prompt_v3",  # 🔥 版本升级：加入页面结构
        model_name=getattr(self.llm, "model", ""),
    )
    cached = self.cache.get(cache_key)
    if isinstance(cached, dict):
        log_agent_action(self.name, "命中页面结构分析缓存", normalized_url[:80])
        log_debug_metrics("llm_cache.page_analysis", self.cache.snapshot_stats())
        return cached

    # 截断前先深度清洗（移除冗余属性）
    if len(html) > 100000:
        html = html[:100000] + "\n... (truncated)"

    # 深度清洗HTML，移除class/style/data-*等噪音属性
    html_cleaned = self._clean_html_for_llm(html)
    original_len = len(html)
    cleaned_len = len(html_cleaned)
    reduction_pct = (1 - cleaned_len / original_len) * 100 if original_len > 0 else 0
    log_agent_action(
        self.name,
        "HTML清洗完成",
        f"原始: {original_len} 字符, 清洗后: {cleaned_len} 字符, 减少: {reduction_pct:.1f}%"
    )

    # 🔥 修改：同时传入清洗后的HTML和页面结构
    response = self.llm.chat_with_system(
        system_prompt=PAGE_ANALYSIS_PROMPT.format(
            task_description=task_description,
            html_content=html_cleaned,
            page_structure=page_structure_text,  # 🔥 新增
            current_url=url_r.data or "",
        ),
        user_message="请分析页面结构并返回选择器配置",
        temperature=0.2, json_mode=True,
    )
    try:
        config = self.llm.parse_json_response(response)
        if config.get("item_selector"):
            self.cache.set(
                cache_key,
                config,
                settings.PAGE_ANALYSIS_CACHE_TTL_SECONDS,
            )
            log_debug_metrics("llm_cache.page_analysis", self.cache.snapshot_stats())
        log_agent_action(self.name, "页面分析完成", f"item_selector: {config.get('item_selector', 'N/A')}")
        return config
    except Exception as e:
        log_error(f"页面分析失败: {e}")
        return {"success": False, "error": str(e)}
```

#### Step 1.2: 更新 PAGE_ANALYSIS_PROMPT

**位置：** `agents/web_worker.py:138-179` (或 `prompts/page_analysis.txt`)

**在 Prompt 中添加页面结构部分：**

```python
PAGE_ANALYSIS_PROMPT = """你是一个网页结构分析专家。请分析以下 HTML，找出目标数据的 CSS 选择器。

## 任务目标
{task_description}

## 页面结构概览（由页面感知器提取）
{page_structure}

## 页面 HTML (已清洗，移除了噪音)
```html
{html_content}
```

## 页面当前 URL
{current_url}

## 你的工作方式
1. **先看页面结构概览**：理解页面的整体布局和内容组织方式
2. **再看 HTML 细节**：找出包含目标数据的重复元素（列表项、表格行等）
3. **为每个需要提取的字段确定精确的 CSS 选择器**
4. **如果页面结构不明确，给出你最有把握的选择器**

返回 JSON 格式：
```json
{
    "success": true,
    "item_selector": "每一条数据项的选择器（如 tr, li, div.item 等）",
    "fields": {
        "title": "标题文本的选择器（相对于 item）",
        "link": "链接的选择器（相对于 item，a 标签会自动提取 href）",
        "date": "日期的选择器（可选）",
        "severity": "严重程度/等级的选择器（可选）",
        "id": "编号/ID 的选择器（可选）"
    },
    "need_click_first": false,
    "click_selector": "如果需要先点击某元素才能看到数据，填写选择器",
    "notes": "其他注意事项"
}
```

注意：
- item_selector 应该能选中多个重复的数据项
- fields 中的选择器是相对于每个 item 的
- 只填你在 HTML 或页面结构中确实看到的选择器，看不到的字段留空字符串
- 优先使用页面结构概览中提到的选择器
"""
```

#### Step 1.3: 测试验证

**创建测试脚本：** `tests/test_web_worker_perception.py`

```python
"""测试 web_worker 的页面感知能力"""
import asyncio
from agents.web_worker import WebWorker
from utils.browser_toolkit import BrowserToolkit

async def test_hacker_news():
    """测试 Hacker News 页面分析"""
    worker = WebWorker()

    async with BrowserToolkit(headless=False) as tk:
        await tk.goto("https://news.ycombinator.com")

        config = await worker.analyze_page_structure(
            tk,
            "提取首页前 10 条新闻的标题和链接"
        )

        print("=" * 60)
        print("页面分析结果：")
        print(f"item_selector: {config.get('item_selector')}")
        print(f"fields: {config.get('fields')}")
        print("=" * 60)

        # 尝试提取数据
        data = await worker.extract_data_with_selectors(tk, config, limit=10)
        print(f"\n提取到 {len(data)} 条数据：")
        for i, item in enumerate(data[:3], 1):
            print(f"{i}. {item.get('title', 'N/A')}")

if __name__ == "__main__":
    asyncio.run(test_hacker_news())
```

**运行测试：**
```bash
cd /Users/zhangwenjun/zwj_project/OmniCore
source .venv/bin/activate
python tests/test_web_worker_perception.py
```

**预期结果：**
- 能看到页面结构概览（标题、列表、段落等）
- 生成的选择器更准确
- 提取到的数据更完整

---

## 阶段2：修复 browser_agent 的内容盲区（优先级：🟠 高）

### 目标
让 `browser_agent.py` 在决策时能看到页面内容，而不只是按钮列表。

### 修改文件
- `agents/browser_agent.py`
- `prompts/browser_page_assessment.txt`

### 具体步骤

#### Step 2.1: 扩展 `_extract_interactive_elements` 方法

**位置：** `agents/browser_agent.py:749-900`

**当前问题：** 只提取交互元素，虽然已经添加了 `context_before` 和 `context_after`，但仍然缺少页面的整体内容结构。

**修改方案：** 在提取交互元素后，额外提取内容元素

```python
async def _extract_page_content_summary(self) -> str:
    """
    提取页面内容摘要（标题、列表、表格、段落）
    用于补充交互元素的上下文
    """
    r = await self.toolkit.evaluate_js(
        r"""
        () => {
            function cleanText(text) {
                return (text || '').replace(/\s+/g, ' ').trim();
            }

            function isVisible(el) {
                const rects = el.getClientRects();
                return !!(el.offsetWidth || el.offsetHeight || rects.length);
            }

            const summary = {
                headings: [],
                lists: [],
                tables: [],
                paragraphs: []
            };

            // 提取标题
            const headings = document.querySelectorAll('h1, h2, h3');
            for (const h of headings) {
                if (!isVisible(h)) continue;
                const text = cleanText(h.textContent);
                if (text.length > 3 && text.length < 200) {
                    summary.headings.push({
                        level: h.tagName.toLowerCase(),
                        text: text
                    });
                }
                if (summary.headings.length >= 10) break;
            }

            // 提取列表
            const lists = document.querySelectorAll('ul, ol');
            for (const list of lists) {
                if (!isVisible(list)) continue;
                const items = Array.from(list.querySelectorAll('li'))
                    .filter(isVisible)
                    .map(li => cleanText(li.textContent))
                    .filter(t => t.length > 0 && t.length < 300);

                if (items.length >= 3) {
                    summary.lists.push({
                        itemCount: items.length,
                        preview: items.slice(0, 3).join(' | ')
                    });
                }
                if (summary.lists.length >= 5) break;
            }

            // 提取表格
            const tables = document.querySelectorAll('table');
            for (const table of tables) {
                if (!isVisible(table)) continue;
                const rows = table.querySelectorAll('tr');
                if (rows.length < 2) continue;

                const headers = Array.from(table.querySelectorAll('th'))
                    .map(th => cleanText(th.textContent))
                    .filter(Boolean);

                summary.tables.push({
                    rowCount: rows.length,
                    headers: headers.join(', ')
                });
                if (summary.tables.length >= 3) break;
            }

            // 提取段落（只取主要内容区域）
            const mainContent = document.querySelector('main, article, [role="main"], .content, #content');
            const paragraphs = (mainContent || document).querySelectorAll('p');
            for (const p of paragraphs) {
                if (!isVisible(p)) continue;
                const text = cleanText(p.textContent);
                if (text.length > 20 && text.length < 500) {
                    summary.paragraphs.push(text.slice(0, 200));
                }
                if (summary.paragraphs.length >= 5) break;
            }

            return summary;
        }
        """
    )

    if not r.success or not r.data:
        return ""

    data = r.data
    lines = ["## 页面内容概览"]

    if data.get("headings"):
        lines.append("\n### 标题")
        for h in data["headings"][:5]:
            lines.append(f"- [{h['level']}] {h['text']}")

    if data.get("lists"):
        lines.append("\n### 列表")
        for lst in data["lists"][:3]:
            lines.append(f"- 列表 ({lst['itemCount']} 项): {lst['preview']}")

    if data.get("tables"):
        lines.append("\n### 表格")
        for tbl in data["tables"][:2]:
            lines.append(f"- 表格 ({tbl['rowCount']} 行): {tbl['headers']}")

    if data.get("paragraphs"):
        lines.append("\n### 段落")
        for p in data["paragraphs"][:3]:
            lines.append(f"- {p}")

    return "\n".join(lines)
```

#### Step 2.2: 修改决策逻辑，加入内容摘要

**位置：** `agents/browser_agent.py:1500-1600` (决策相关方法)

**在调用 LLM 决策前，先获取内容摘要：**

```python
# 在 _assess_page_and_decide 或类似方法中
async def _assess_page_and_decide(self, task: str, intent: TaskIntent) -> Optional[BrowserAction]:
    # ... 现有代码 ...

    # 🔥 新增：获取页面内容摘要
    content_summary = await self._extract_page_content_summary()

    # 修改 Prompt，加入内容摘要
    prompt = PAGE_ASSESSMENT_PROMPT.format(
        task=task,
        intent=intent.intent_type,
        query=intent.query,
        url=current_url,
        title=title,
        last_action=last_action_desc,
        data=visible_data_text,
        elements=elements_text,
        content_summary=content_summary,  # 🔥 新增
    )

    # ... 后续代码 ...
```

#### Step 2.3: 更新 PAGE_ASSESSMENT_PROMPT

**位置：** `prompts/browser_page_assessment.txt`

**在 Prompt 中添加内容摘要部分：**

```
You are evaluating whether the current browser page already advances the task.

Task: {task}
Intent: {intent}
Query: {query}
Current URL: {url}
Page title: {title}
Last action: {last_action}

## 页面内容概览
{content_summary}

## 可见数据
{data}

## 可交互元素
{elements}

Rules:
- First understand the page content overview to get context
- Use the content overview to understand what each interactive element does
- If the visible data already likely answers the task, choose action.type="extract"
- If the task is already complete without further extraction, choose action.type="done"
- If the page is a search results page and the snippets are relevant enough to answer the task, prefer "extract" over clicking through
- If the current page is relevant but incomplete and a candidate likely leads to the best source or detail page, choose "click" and reference the element_index
- Choose "input" only if the current page is not already showing relevant results or the search box clearly needs a different query
- If you cannot justify a meaningful action from the evidence, choose "wait"

Return JSON only with keys:
- page_relevant: boolean
- goal_satisfied: boolean
- reason: string
- evidence_indexes: array of integers
- confidence: number
- action: object with keys type, element_index, target_selector, value, description, fallback_selector, use_keyboard, keyboard_key
```

---

## 阶段3：集成三层感知架构（优先级：🟡 中）

### 目标
让系统在复杂场景下使用 `EnhancedWebWorker` 的三层感知能力。

### 修改文件
- `core/tool_registry.py`
- `core/tool_adapters.py`
- `core/router.py`

### 具体步骤

#### Step 3.1: 注册 EnhancedWebWorker 为新工具

**位置：** `core/tool_registry.py:225-250`

**在 `_register_builtin_tools` 中添加：**

```python
def _register_builtin_tools(registry: ToolRegistry) -> None:
    # ... 现有工具注册 ...

    # 🔥 新增：注册增强版 Web Worker
    registry.register(
        RegisteredTool(
            spec=ToolSpec(
                name="web.smart_extract",
                task_type="enhanced_web_worker",
                description="Smart web extraction with three-layer perception (understanding → selector → extraction). Use for complex pages.",
                risk_level="low",
                tags=["web", "scraping", "smart", "perception"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "task": {"type": "string"},
                        "url": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                },
            ),
            adapter_name="enhanced_web_worker",
            max_parallelism=2,
        )
    )
```

#### Step 3.2: 创建 EnhancedWebWorker 适配器

**位置：** `core/tool_adapters.py` (在文件末尾添加)

```python
@tool_adapter("enhanced_web_worker")
class EnhancedWebWorkerAdapter(BaseToolAdapter):
    """Adapter for EnhancedWebWorker with three-layer perception"""

    async def execute(
        self,
        task: Dict[str, Any],
        shared_memory_snapshot: Dict[str, Any],
        registered_tool: RegisteredTool,
    ) -> Dict[str, Any]:
        from agents.enhanced_web_worker import EnhancedWebWorker
        from utils.browser_toolkit import BrowserToolkit

        params = task.get("params", {})
        task_description = params.get("task", "")
        url = params.get("url", "")
        limit = params.get("limit", 10)

        if not task_description:
            return _base_outcome(
                task,
                registered_tool,
                status=str(TaskStatus.FAILED),
                result={"success": False, "error": "Missing task description"},
                shared_memory=None,
                error_trace="Missing task description",
                failure_type=str(FailureType.INVALID_INPUT),
            )

        worker = EnhancedWebWorker()

        try:
            async with BrowserToolkit(
                headless=settings.BROWSER_FAST_MODE,
                fast_mode=settings.BROWSER_FAST_MODE,
                block_heavy_resources=settings.BLOCK_HEAVY_RESOURCES,
            ) as toolkit:
                if url:
                    await toolkit.goto(url)

                result = await worker.smart_extract(
                    toolkit=toolkit,
                    task_description=task_description,
                    limit=limit
                )

                if result.get("success"):
                    return _base_outcome(
                        task,
                        registered_tool,
                        status=str(TaskStatus.COMPLETED),
                        result=result,
                        shared_memory=result.get("data", []),
                        error_trace="",
                        failure_type=None,
                    )
                else:
                    return _base_outcome(
                        task,
                        registered_tool,
                        status=str(TaskStatus.FAILED),
                        result=result,
                        shared_memory=None,
                        error_trace=result.get("error", "Unknown error"),
                        failure_type=str(FailureType.UNKNOWN),
                    )
        except Exception as e:
            return _base_outcome(
                task,
                registered_tool,
                status=str(TaskStatus.FAILED),
                result={"success": False, "error": str(e)},
                shared_memory=None,
                error_trace=str(e),
                failure_type=classify_failure(str(e)),
            )
```

#### Step 3.3: 让 Router 智能选择工具

**位置：** `prompts/router_system.txt` (或 `core/router.py` 中的 ROUTER_SYSTEM_PROMPT)

**在工具选择规则中添加：**

```
## Tool Selection Guidelines

For web scraping tasks:
- Use `web.smart_extract` for:
  - Complex pages with unclear structure
  - Government/enterprise websites
  - Pages with dynamic content
  - When previous `web.fetch_and_extract` failed

- Use `web.fetch_and_extract` for:
  - Simple, well-structured pages
  - Known websites (Hacker News, GitHub, etc.)
  - Quick data extraction

For browser interaction:
- Use `browser.interact` for:
  - Multi-step workflows
  - Form filling
  - Login required
  - JavaScript-heavy pages
```

---

## 📝 测试计划

### 测试用例1：简单列表页（Hacker News）
```bash
python main.py "去 Hacker News 抓取前 10 条新闻标题和链接"
```

**预期结果：**
- 阶段1修复后：能正确识别列表结构，生成准确的选择器
- 提取到完整的标题和链接

### 测试用例2：复杂政府网站（CNNVD）
```bash
python main.py "去 CNNVD 查询最近7天的高危漏洞"
```

**预期结果：**
- 阶段1修复后：能理解页面结构，找到漏洞列表
- 阶段2修复后：能看到漏洞描述和严重等级
- 阶段3修复后：自动使用 `web.smart_extract`

### 测试用例3：搜索结果页
```bash
python main.py "搜索'Claude AI'的最新新闻"
```

**预期结果：**
- 阶段2修复后：能理解搜索结果的内容，而不只是看到"点击"按钮
- 能判断是否需要点击进入详情页

---

## 🚨 风险控制

### 降级策略
每个阶段都保留降级路径：

```python
# 阶段1：如果 PagePerceiver 失败
try:
    page_structure = await perceiver.perceive_page(tk, task_description)
except Exception as e:
    log_warning(f"页面结构提取失败，降级为纯HTML分析: {e}")
    page_structure_text = "(页面结构提取失败，仅使用HTML分析)"
```

```python
# 阶段2：如果内容摘要提取失败
try:
    content_summary = await self._extract_page_content_summary()
except Exception as e:
    log_warning(f"内容摘要提取失败: {e}")
    content_summary = "(内容摘要不可用)"
```

```python
# 阶段3：如果 EnhancedWebWorker 失败
if result.get("success") == False:
    log_warning("EnhancedWebWorker 失败，回退到标准 WebWorker")
    # 回退到 web.fetch_and_extract
```

### 回滚方案
每个阶段都可以独立回滚：

- **阶段1回滚：** 将 `prompt_version` 改回 `"page_analysis_prompt_v2"`，移除 `page_structure` 参数
- **阶段2回滚：** 移除 `_extract_page_content_summary` 调用，恢复原 Prompt
- **阶段3回滚：** 从 `tool_registry` 中移除 `web.smart_extract` 注册

---

## 📊 预期效果

| 指标 | 修复前 | 阶段1后 | 阶段2后 | 阶段3后 |
|------|--------|---------|---------|---------|
| 选择器准确率 | 60% | 85% | 85% | 90% |
| 复杂页面成功率 | 40% | 70% | 80% | 90% |
| Token 使用量 | 25k | 8k | 10k | 12k |
| 平均执行时间 | 15s | 12s | 15s | 20s |

---

## 🎯 执行建议

1. **先执行阶段1**（最重要，风险最低）
   - 预计30分钟完成
   - 立即测试 Hacker News 用例
   - 如果效果好，再继续

2. **观察1-2天后执行阶段2**
   - 确保阶段1稳定
   - 预计1小时完成
   - 测试 CNNVD 等复杂网站

3. **根据需要决定是否执行阶段3**
   - 如果阶段1+2已经解决大部分问题，可以暂缓
   - 阶段3主要是架构优化，不是必需的

---

## 📌 关键注意事项

1. **缓存版本升级**：每次修改 Prompt 后，记得升级 `prompt_version`，否则会命中旧缓存
2. **日志观察**：修改后重点观察日志中的"页面结构提取"和"HTML清洗"信息
3. **逐步验证**：不要一次性修改所有文件，先改一个，测试通过后再改下一个
4. **保留旧代码**：用注释标记修改位置，方便回滚

---

## 🔗 相关文件清单

### 需要修改的文件
- [ ] `agents/web_worker.py` (阶段1)
- [ ] `prompts/page_analysis.txt` (阶段1，如果独立存在)
- [ ] `agents/browser_agent.py` (阶段2)
- [ ] `prompts/browser_page_assessment.txt` (阶段2)
- [ ] `core/tool_registry.py` (阶段3)
- [ ] `core/tool_adapters.py` (阶段3)

### 已存在的工具（无需修改）
- ✅ `utils/page_perceiver.py`
- ✅ `agents/enhanced_web_worker.py`
- ✅ `prompts/page_perception.txt`

### 需要创建的测试文件
- [ ] `tests/test_web_worker_perception.py`
- [ ] `tests/test_browser_agent_content.py`
- [ ] `tests/test_enhanced_worker_integration.py`
