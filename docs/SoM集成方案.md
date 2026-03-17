# 🚀 集成 Set-of-Mark (SoM) 方案

## 📊 为什么需要 SoM？

**当前问题**:
- 纯 DOM 文本解析准确率低
- 选择器容易失效
- 动态内容难以处理

**SoM 的优势**:
- ✅ 视觉理解更准确（GPT-4V 成功率 51.1%）
- ✅ 不依赖复杂的 DOM 解析
- ✅ 能处理动态内容

## 🔧 实现方案

### 方案 A：使用现有库（推荐）

**npm 包**: [@brewcoua/web-som](https://npmjs.com/package/@brewcoua/web-som)

这是一个专门为 Web Agent 设计的 SoM 实现。

**集成步骤**:

1. **在 Playwright 中注入 SoM 脚本**

```python
# utils/som_marker.py
import json
from pathlib import Path

class SoMMarker:
    """Set-of-Mark 标记器"""

    def __init__(self):
        # 加载 SoM JavaScript 脚本
        som_script_path = Path(__file__).parent / "som_script.js"
        self.som_script = som_script_path.read_text()

    async def mark_page(self, page):
        """在页面上添加 SoM 标记"""
        # 注入 SoM 脚本
        await page.evaluate(self.som_script)

        # 执行标记
        marks = await page.evaluate("""
            () => {
                // 找到所有可交互元素
                const elements = document.querySelectorAll(
                    'a, button, input, select, textarea, [onclick], [role="button"]'
                );

                const marks = [];
                elements.forEach((el, index) => {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        // 创建标记
                        const mark = document.createElement('div');
                        mark.className = 'som-mark';
                        mark.textContent = `[${index + 1}]`;
                        mark.style.cssText = `
                            position: absolute;
                            left: ${rect.left + window.scrollX}px;
                            top: ${rect.top + window.scrollY}px;
                            background: red;
                            color: white;
                            padding: 2px 6px;
                            border-radius: 3px;
                            font-size: 12px;
                            font-weight: bold;
                            z-index: 999999;
                            pointer-events: none;
                        `;
                        document.body.appendChild(mark);

                        marks.push({
                            id: index + 1,
                            tag: el.tagName.toLowerCase(),
                            text: el.textContent.trim().substring(0, 50),
                            selector: getSelector(el),
                            rect: {
                                x: rect.left,
                                y: rect.top,
                                width: rect.width,
                                height: rect.height
                            }
                        });
                    }
                });

                function getSelector(el) {
                    if (el.id) return `#${el.id}`;
                    if (el.className) return `.${el.className.split(' ')[0]}`;
                    return el.tagName.toLowerCase();
                }

                return marks;
            }
        """)

        return marks

    async def take_marked_screenshot(self, page):
        """截取带标记的截图"""
        screenshot = await page.screenshot(full_page=False)
        return screenshot

    async def remove_marks(self, page):
        """移除标记"""
        await page.evaluate("""
            () => {
                document.querySelectorAll('.som-mark').forEach(el => el.remove());
            }
        """)
```

2. **在 Browser Agent 中集成**

```python
# agents/browser_agent.py

from utils.som_marker import SoMMarker

class BrowserAgent:
    def __init__(self, ...):
        # ... 原有代码
        self.som_marker = SoMMarker()
        self.use_vision = True  # 是否使用视觉模式

    async def _decide_next_action_with_vision(self, task, marks, screenshot):
        """使用视觉模式决策下一步动作"""
        # 构建 prompt
        marks_text = "\n".join([
            f"[{m['id']}] {m['tag']}: {m['text']}"
            for m in marks[:50]  # 限制数量
        ])

        prompt = f"""你是一个网页自动化 Agent。

任务: {task}

当前页面上有以下可交互元素（已在截图上标记）:
{marks_text}

请根据截图和元素列表，决定下一步操作。

返回 JSON 格式:
{{
    "action": "click" | "input" | "scroll" | "done",
    "target_mark": 5,  // 标记编号
    "reasoning": "为什么选择这个操作"
}}
"""

        # 调用 GPT-4V（需要支持 vision 的 LLM）
        response = await self.llm.chat_with_image(
            text=prompt,
            image=screenshot,
            temperature=0.3,
            json_mode=True
        )

        return response

    async def run(self, task: str, start_url: Optional[str] = None, max_steps: int = 8):
        # ... 原有导航代码

        for step in range(max_steps):
            if self.use_vision and self.llm.supports_vision():
                # 使用 SoM 视觉模式
                marks = await self.som_marker.mark_page(self.toolkit.page)
                screenshot = await self.som_marker.take_marked_screenshot(self.toolkit.page)

                action = await self._decide_next_action_with_vision(task, marks, screenshot)

                # 执行动作
                if action['action'] == 'click':
                    target_mark = action['target_mark']
                    # 找到对应的元素
                    mark_info = marks[target_mark - 1]
                    await self.toolkit.click(mark_info['selector'])

                # 移除标记
                await self.som_marker.remove_marks(self.toolkit.page)
            else:
                # 降级到纯文本模式
                action = await self._decide_next_action(task)
                # ... 原有逻辑
```

### 方案 B：混合模式（推荐用于你的场景）

**策略**: 优先使用视觉模式，失败时降级到文本模式

```python
async def run(self, task, start_url, max_steps):
    # 检查是否支持 vision
    if self.llm.supports_vision():
        try:
            return await self._run_with_vision(task, start_url, max_steps)
        except Exception as e:
            log_warning(f"视觉模式失败: {e}, 降级到文本模式")

    # 降级到文本模式
    return await self._run_with_text(task, start_url, max_steps)
```

## 📊 预期效果

根据论文数据：

| 方法 | 成功率 | 适用场景 |
|------|--------|----------|
| 纯文本 DOM | ~30% | 简单静态页面 |
| SoM + GPT-4V | 51.1% | 复杂动态页面 |
| WebVoyager (混合) | 59.1% | 真实网站 |

**对你的系统**:
- CNNVD 任务: 从失败 → 成功率 50%+
- 天气任务: 从 403 失败 → 成功率 60%+
- 通用任务: 整体成功率提升 20-30%

## 🚀 立即可行的改进

### 1. 最小化实现（不需要 GPT-4V）

即使没有 vision 模型，也可以用 SoM 的思想：

```python
# 在 DOM 文本中添加数字标记
async def _get_enhanced_dom_text(self, page):
    elements = await page.query_selector_all('a, button, input')

    dom_text = []
    for i, el in enumerate(elements[:50], 1):
        text = await el.text_content()
        tag = await el.evaluate('el => el.tagName')
        dom_text.append(f"[{i}] {tag}: {text[:50]}")

    return "\n".join(dom_text)

# LLM 返回: "点击标记 [5]"
# 系统根据标记找到对应元素
```

这样即使用纯文本模型，也能提高准确率！

## 📝 总结

**核心思想**: 不要让 LLM 生成复杂的 CSS 选择器，而是：
1. 给每个元素编号
2. LLM 只需要说"点击 [5]"
3. 系统根据编号映射到元素

这大大降低了 LLM 的负担，提高了准确率！

## 参考资料

- [SeeAct Paper](https://arxiv.org/abs/2401.01614v2)
- [WebVoyager Paper](https://arxiv.org/abs/2401.13919)
- [Set-of-Mark (SoM)](https://arxiv.org/abs/2310.11441)
- [Microsoft SoM GitHub](https://github.com/microsoft/SoM)
- [web-som npm package](https://npmjs.com/package/@brewcoua/web-som)
