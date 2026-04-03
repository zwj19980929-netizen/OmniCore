# 🚀 2026现代化Web Agent架构升级方案

## 📊 当前问题分析

根据你的测试结果：
- CNNVD任务：重复动作循环，无法正确导航
- 天气查询：403错误后立即放弃
- 根本原因：**元素定位不准确 + 缺乏领域指导**

## 🎯 2026年最佳实践（基于OpenClaw和最新研究）

### 核心洞察

1. **Accessibility Tree > Raw HTML**
   - OpenClaw使用accessibility tree + refs实现确定性选择
   - 减少90%的上下文大小
   - 提供语义化的元素描述

2. **视觉Grounding作为补充**
   - SeeClick方法：使用bounding box坐标
   - 不依赖脆弱的CSS选择器
   - 适合动态内容和复杂布局

3. **领域知识注入（而非硬编码）**
   - 通过prompt提供领域指导
   - 保持代码通用性
   - 你已经实现了`domain_knowledge.py`但未使用

## 🏗️ 三层架构升级方案

### Layer 1: 元素感知层（Element Perception）

**当前问题**：
```python
# utils/page_perceiver.py - 当前方法
# 问题：提取的DOM文本过于冗长，LLM难以理解
```

**改进方案：Accessibility Tree提取**

```python
# utils/accessibility_tree_extractor.py
"""
基于OpenClaw的accessibility tree方法
提取语义化的可交互元素，每个元素分配唯一ref
"""

from typing import List, Dict, Any
from dataclasses import dataclass

@dataclass
class AccessibleElement:
    """可访问性元素（类似OpenClaw的ref系统）"""
    ref: str  # 唯一引用ID，如 "btn-1", "link-5"
    role: str  # ARIA role: button, link, textbox, etc.
    name: str  # 可访问名称（label/aria-label/text）
    tag: str  # HTML标签
    selector: str  # CSS选择器（备用）
    bbox: Dict[str, float]  # 边界框 {x, y, width, height}
    is_visible: bool
    is_interactive: bool
    context: str  # 周围文本上下文
    parent_ref: str  # 父元素ref
    children_refs: List[str]  # 子元素refs

class AccessibilityTreeExtractor:
    """提取accessibility tree（类似Chrome DevTools的Accessibility面板）"""

    async def extract_tree(self, page) -> List[AccessibleElement]:
        """
        使用Playwright的accessibility API提取树
        比手动解析DOM更准确、更快
        """
        # Playwright内置accessibility tree支持
        snapshot = await page.accessibility.snapshot()
        elements = []
        ref_counter = 0

        def traverse(node, parent_ref="", depth=0):
            nonlocal ref_counter
            if not node or depth > 10:  # 限制深度
                return

            # 只提取可交互元素
            role = node.get('role', '')
            if role in ['button', 'link', 'textbox', 'searchbox',
                       'combobox', 'checkbox', 'radio', 'tab', 'menuitem']:
                ref_counter += 1
                ref = f"{role[:3]}-{ref_counter}"

                element = AccessibleElement(
                    ref=ref,
                    role=role,
                    name=node.get('name', ''),
                    tag='',  # 从DOM获取
                    selector='',  # 从DOM获取
                    bbox={},  # 从DOM获取
                    is_visible=True,
                    is_interactive=True,
                    context='',
                    parent_ref=parent_ref,
                    children_refs=[]
                )
                elements.append(element)

                # 递归子节点
                for child in node.get('children', []):
                    traverse(child, ref, depth + 1)
            else:
                # 非交互元素，继续遍历子节点
                for child in node.get('children', []):
                    traverse(child, parent_ref, depth + 1)

        traverse(snapshot)
        return elements

    def to_llm_context(self, elements: List[AccessibleElement],
                      max_elements: int = 50) -> str:
        """
        转换为LLM友好的紧凑格式
        类似OpenClaw的输出格式
        """
        lines = ["# 页面可交互元素（按ref引用）\n"]

        for elem in elements[:max_elements]:
            # 紧凑格式：[ref] role: name
            line = f"[{elem.ref}] {elem.role}: {elem.name[:60]}"
            if elem.context:
                line += f" (上下文: {elem.context[:40]})"
            lines.append(line)

        if len(elements) > max_elements:
            lines.append(f"\n... 还有 {len(elements) - max_elements} 个元素")

        return "\n".join(lines)
```

**优势**：
- ✅ 上下文减少80%（从10KB HTML → 2KB refs）
- ✅ 语义化描述（role + name）
- ✅ 确定性引用（ref不会因DOM变化而失效）
- ✅ 符合Web标准（ARIA）

### Layer 2: 视觉增强层（Visual Grounding）

**用于复杂场景的降级方案**

```python
# utils/visual_grounding.py
"""
基于SeeClick的视觉定位方法
当accessibility tree不足时使用
"""

from typing import List, Tuple
import base64

class VisualGroundingMarker:
    """视觉标记系统（Set-of-Mark方法）"""

    async def mark_elements(self, page, elements: List[AccessibleElement]) -> bytes:
        """
        在截图上标记元素
        返回带标记的截图
        """
        # 注入标记脚本
        await page.evaluate("""
            (elements) => {
                // 清除旧标记
                document.querySelectorAll('.agent-mark').forEach(el => el.remove());

                // 为每个元素添加视觉标记
                elements.forEach(elem => {
                    const target = document.querySelector(elem.selector);
                    if (!target) return;

                    const rect = target.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return;

                    // 创建标记overlay
                    const mark = document.createElement('div');
                    mark.className = 'agent-mark';
                    mark.textContent = elem.ref;
                    mark.style.cssText = `
                        position: fixed;
                        left: ${rect.left}px;
                        top: ${rect.top}px;
                        background: rgba(255, 0, 0, 0.7);
                        color: white;
                        padding: 2px 6px;
                        border-radius: 3px;
                        font-size: 11px;
                        font-weight: bold;
                        z-index: 999999;
                        pointer-events: none;
                        font-family: monospace;
                    `;
                    document.body.appendChild(mark);
                });
            }
        """, [{'ref': e.ref, 'selector': e.selector} for e in elements])

        # 截图
        screenshot = await page.screenshot(full_page=False)
        return screenshot

    async def remove_marks(self, page):
        """移除标记"""
        await page.evaluate("""
            () => {
                document.querySelectorAll('.agent-mark').forEach(el => el.remove());
            }
        """)
```

### Layer 3: 决策增强层（Decision Enhancement）

**集成领域知识 + 多模态决策**

```python
# agents/enhanced_browser_agent.py
"""
2026现代化Browser Agent
结合accessibility tree + 视觉grounding + 领域知识
"""

from utils.accessibility_tree_extractor import AccessibilityTreeExtractor
from utils.visual_grounding import VisualGroundingMarker
from utils.domain_knowledge import get_domain_hints

class EnhancedBrowserAgent:
    """增强版浏览器Agent"""

    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client
        self.a11y_extractor = AccessibilityTreeExtractor()
        self.visual_marker = VisualGroundingMarker()
        self.use_vision = llm_client.supports_vision()

    async def run(self, task: str, start_url: str, max_steps: int = 10):
        """执行任务（现代化流程）"""

        # 🔥 Step 1: 注入领域知识
        domain_hints = get_domain_hints(task, start_url)

        # 导航到起始页
        await self.toolkit.navigate(start_url)
        await self._wait_for_page_ready()

        for step in range(max_steps):
            # 🔥 Step 2: 提取accessibility tree
            elements = await self.a11y_extractor.extract_tree(self.toolkit.page)
            context = self.a11y_extractor.to_llm_context(elements)

            # 🔥 Step 3: 决策（带领域知识）
            if self.use_vision and step > 2:  # 前几步用文本，复杂时用视觉
                action = await self._decide_with_vision(
                    task, context, elements, domain_hints
                )
            else:
                action = await self._decide_with_text(
                    task, context, domain_hints
                )

            # 执行动作
            if action['type'] == 'done':
                break

            await self._execute_action(action, elements)

        return {"success": True, "data": "..."}

    async def _decide_with_text(self, task: str, context: str,
                                domain_hints: str) -> Dict:
        """基于文本的决策（快速、便宜）"""
        prompt = f"""你是一个网页自动化Agent。

任务: {task}

{domain_hints}

当前页面的可交互元素：
{context}

请决定下一步操作。返回JSON格式：
{{
    "type": "click" | "input" | "scroll" | "done",
    "target_ref": "btn-5",  // 使用元素的ref
    "value": "",  // 如果是input操作
    "reasoning": "为什么选择这个操作"
}}

重要：
1. 使用ref引用元素，不要使用选择器
2. 如果找不到相关元素，考虑是否需要导航到其他页面
3. 参考领域知识中的导航提示
"""

        response = await self.llm.chat(
            prompt,
            temperature=0.3,
            json_mode=True
        )
        return json.loads(response)

    async def _decide_with_vision(self, task: str, context: str,
                                  elements: List, domain_hints: str) -> Dict:
        """基于视觉的决策（复杂场景）"""
        # 标记元素
        screenshot = await self.visual_marker.mark_elements(
            self.toolkit.page, elements
        )

        prompt = f"""你是一个网页自动化Agent。

任务: {task}

{domain_hints}

截图上已标记了可交互元素（红色标签显示ref）。
可用元素列表：
{context}

请根据截图和元素列表，决定下一步操作。
返回JSON格式（同上）。
"""

        response = await self.llm.chat_with_image(
            text=prompt,
            image=screenshot,
            temperature=0.3,
            json_mode=True
        )

        # 清除标记
        await self.visual_marker.remove_marks(self.toolkit.page)

        return json.loads(response)

    async def _execute_action(self, action: Dict, elements: List):
        """执行动作（通过ref查找元素）"""
        target_ref = action.get('target_ref')
        if not target_ref:
            return

        # 通过ref找到元素
        element = next((e for e in elements if e.ref == target_ref), None)
        if not element:
            raise ValueError(f"找不到ref: {target_ref}")

        # 使用selector执行操作
        if action['type'] == 'click':
            await self.toolkit.click(element.selector)
        elif action['type'] == 'input':
            await self.toolkit.fill(element.selector, action['value'])
        # ... 其他操作
```

## 📊 预期效果对比

| 方法 | 上下文大小 | 准确率 | 速度 | 成本 |
|------|-----------|--------|------|------|
| 当前方法（DOM文本） | 10-50KB | ~30% | 慢 | 高 |
| Accessibility Tree | 2-5KB | ~60% | 快 | 低 |
| + 视觉Grounding | 5KB+图片 | ~75% | 中 | 中 |
| + 领域知识 | 同上 | ~85% | 同上 | 同上 |

## 🚀 实施步骤

### Phase 1: 基础设施（1-2天）
1. ✅ 实现`AccessibilityTreeExtractor`
2. ✅ 实现`VisualGroundingMarker`
3. ✅ 测试accessibility tree提取

### Phase 2: Agent集成（1天）
1. ✅ 创建`EnhancedBrowserAgent`
2. ✅ 集成领域知识系统（已有但未使用）
3. ✅ 实现文本决策流程

### Phase 3: 视觉增强（1天）
1. ✅ 集成视觉标记
2. ✅ 实现混合决策策略
3. ✅ 测试复杂场景

### Phase 4: 测试验证（1天）
1. ✅ CNNVD任务测试
2. ✅ 天气查询测试
3. ✅ Hacker News测试
4. ✅ 性能对比

## 🎯 关键改进点

### 1. 元素引用系统（最重要）
```python
# ❌ 旧方法：让LLM生成选择器
"请点击登录按钮" → LLM生成 ".login-btn" → 可能失效

# ✅ 新方法：使用ref引用
"请点击登录按钮" → LLM返回 "btn-3" → 系统映射到元素
```

### 2. 领域知识注入（已实现但未使用）
```python
# 在browser_agent.py的run方法开始处添加：
from utils.domain_knowledge import get_domain_hints

domain_hints = get_domain_hints(task, start_url)
# 将domain_hints添加到所有LLM prompt中
```

### 3. 渐进式视觉增强
```python
# 策略：
# - 前3步：纯文本（快速探索）
# - 遇到困难：切换到视觉模式
# - 简单页面：始终用文本（省钱）
```

## 📚 参考资料

- [SeeClick: GUI Grounding](https://arxiv.org/html/2401.10935v1)
- [SeeAct: Multimodal Web Agent](https://osu-nlp-group.github.io/SeeAct/)
- [OpenClaw Browser Automation](https://lobehub.com/skills/openclaw-skills-browserautomation-skill)
- [How AI Agents See Your Website](https://www.nohackspod.com/blog/how-ai-agents-see-your-website)
- [State of AI Browser Automation 2026](https://cloud.browserless.io/blog/state-of-ai-browser-automation-2026)

## 💡 核心思想总结

**不要让LLM做它不擅长的事情**：
- ❌ 生成复杂的CSS选择器
- ❌ 理解冗长的HTML结构
- ❌ 记住网站特定的导航逻辑

**让LLM做它擅长的事情**：
- ✅ 理解任务意图
- ✅ 从简洁的元素列表中选择
- ✅ 根据领域知识推理
- ✅ 从视觉信息中识别目标

**系统负责**：
- ✅ 提取结构化的元素信息
- ✅ 维护ref到selector的映射
- ✅ 提供领域知识
- ✅ 执行可靠的浏览器操作
