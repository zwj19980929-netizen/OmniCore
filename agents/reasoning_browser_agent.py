"""
推理式浏览器Agent - 2026现代化架构
基于Chain-of-Thought推理，无需领域知识

核心思想：
1. 让Agent观察页面结构（accessibility tree）
2. 让Agent自己推理页面类型和导航策略
3. 从经验中学习，而不是硬编码知识
"""

import asyncio
import json
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from urllib.parse import urlparse

from core.llm import LLMClient
from utils.browser_toolkit import BrowserToolkit
from utils.accessibility_tree_extractor import (
    AccessibilityTreeExtractor,
    AccessibleElement
)
from utils.enhanced_page_perceiver import EnhancedPagePerceiver
from utils.logger import log_error, log_success, log_warning, console


@dataclass
class ActionDecision:
    """Agent的动作决策"""
    action_type: str  # click, input, scroll, extract, done, failed
    target_ref: str = ""
    value: str = ""
    reasoning: str = ""
    page_analysis: Dict[str, Any] = None
    confidence: float = 0.0


class ReasoningBrowserAgent:
    """
    推理式浏览器Agent

    不依赖预定义的领域知识，而是通过观察和推理来完成任务
    类似人类浏览网页的思考过程
    """

    def __init__(self, llm_client: LLMClient, toolkit: BrowserToolkit):
        self.llm = llm_client
        self.toolkit = toolkit
        self.a11y_extractor = AccessibilityTreeExtractor()
        self.page_perceiver = EnhancedPagePerceiver()  # 增强的页面感知器

        # 记录执行历史（用于上下文）
        self.action_history: List[Dict] = []
        self.visited_urls: List[str] = []

    async def run(self, task: str, start_url: str, max_steps: int = 10) -> Dict[str, Any]:
        """
        执行任务

        Args:
            task: 任务描述（自然语言）
            start_url: 起始URL
            max_steps: 最大步数

        Returns:
            执行结果
        """
        console.print(f"[cyan]🧠 推理式Agent启动[/cyan]")
        console.print(f"[cyan]任务: {task}[/cyan]")
        console.print(f"[cyan]起始URL: {start_url}[/cyan]")

        # 重置状态
        self.action_history = []
        self.visited_urls = []

        # 导航到起始页
        nav_result = await self.toolkit.goto(start_url)
        if not nav_result.success:
            return {
                "success": False,
                "error": f"无法导航到起始URL: {nav_result.error}"
            }

        await self._wait_for_page_ready()
        current_url = start_url
        self.visited_urls.append(current_url)

        # 主循环
        for step in range(max_steps):
            console.print(f"\n{'='*60}")
            console.print(f"[cyan]📍 Step {step + 1}/{max_steps}[/cyan]")
            console.print(f"{'='*60}")

            # 1. 感知：提取页面结构和内容
            try:
                page_content = await self.page_perceiver.perceive_page(self.toolkit.page)
                log_success(f"提取到 {len(page_content.interactive_elements)} 个可交互元素")
                log_success(f"页面摘要: {page_content.page_summary}")
            except Exception as e:
                log_error(f"提取页面结构失败: {e}")
                return {
                    "success": False,
                    "error": f"页面结构提取失败: {str(e)}"
                }

            # 2. 推理：分析情况并决策
            try:
                decision = await self._analyze_and_decide(
                    task=task,
                    page_content=page_content,
                    current_url=current_url,
                    step=step
                )

                console.print(f"[cyan]🤔 推理结果:[/cyan]")
                console.print(f"   页面类型: {decision.page_analysis.get('page_type', 'unknown')}")
                console.print(f"   决策: {decision.action_type}")
                console.print(f"   理由: {decision.reasoning}")

            except Exception as e:
                log_error(f"推理失败: {e}")
                return {
                    "success": False,
                    "error": f"推理失败: {str(e)}"
                }

            # 3. 执行：根据决策执行动作
            if decision.action_type == "done":
                # 任务完成，提取数据
                log_success("✅ Agent认为任务已完成，开始提取数据")
                extraction_result = await self._extract_data(task, page_content)

                return {
                    "success": True,
                    "data": extraction_result,
                    "steps_taken": len(self.action_history),
                    "action_history": self.action_history
                }

            elif decision.action_type == "failed":
                # 任务失败
                log_error(f"❌ Agent认为任务无法完成: {decision.reasoning}")
                return {
                    "success": False,
                    "error": decision.reasoning,
                    "steps_taken": len(self.action_history),
                    "action_history": self.action_history
                }

            else:
                # 执行动作
                try:
                    execution_result = await self._execute_action(decision, page_content.interactive_elements)

                    if not execution_result["success"]:
                        log_warning(f"动作执行失败: {execution_result.get('error')}")
                        # 继续尝试，不立即放弃
                    else:
                        log_success(f"✓ 动作执行成功")

                    # 记录历史
                    self.action_history.append({
                        "step": step + 1,
                        "action": decision.action_type,
                        "target_ref": decision.target_ref,
                        "reasoning": decision.reasoning,
                        "url": current_url,
                        "success": execution_result["success"]
                    })

                    # 更新当前URL
                    await asyncio.sleep(1.5)  # 等待页面响应
                    url_result = await self.toolkit.get_current_url()
                    if url_result.success:
                        new_url = url_result.data
                        if new_url != current_url:
                            console.print(f"[cyan]🔄 页面跳转: {new_url}[/cyan]")
                            current_url = new_url
                            self.visited_urls.append(current_url)

                except Exception as e:
                    log_error(f"执行动作时出错: {e}")
                    # 继续尝试下一步

        # 达到最大步数
        log_warning(f"⚠️ 达到最大步数 {max_steps}，任务未完成")
        return {
            "success": False,
            "error": f"达到最大步数 {max_steps}",
            "steps_taken": len(self.action_history),
            "action_history": self.action_history
        }

    async def _analyze_and_decide(
        self,
        task: str,
        page_content,  # PageContent对象
        current_url: str,
        step: int
    ) -> ActionDecision:
        """
        核心推理方法：分析当前情况并做出决策

        使用Chain-of-Thought让LLM逐步推理
        """

        # 构建完整的页面上下文（包含结构、内容、元素）
        full_context = self.page_perceiver.to_llm_context(page_content)

        # 构建历史上下文
        history_context = self._build_history_context()

        # Chain-of-Thought推理prompt
        prompt = f"""你是一个网页导航专家Agent。你需要通过观察和推理来完成任务。

## 任务目标
{task}

## 当前状态
- 当前URL: {current_url}
- 当前步骤: {step + 1}
- 已访问的URL: {', '.join(self.visited_urls[-3:])}

## 完整的页面信息
{full_context}

## 之前的操作历史
{history_context}

---

请按照以下步骤进行**逐步推理**（Chain-of-Thought）：

### Step 1: 页面类型识别
观察页面的标题、内容和元素，判断这是什么类型的页面？

可能的类型：
- **首页/门户页**: 有导航菜单、分类链接，但没有具体的数据列表
- **列表页**: 有多条相似的数据项（如新闻列表、产品列表、搜索结果）
- **详情页**: 单个项目的详细信息
- **搜索页**: 有搜索框和搜索按钮
- **搜索结果页**: 显示搜索结果列表
- **表单页**: 有多个输入框和提交按钮
- **错误页**: 404、403等错误页面

判断依据：
- 页面标题和主要标题
- 页面内容的类型和结构
- 可交互元素的类型和数量
- URL特征

### Step 2: 任务需求分析
要完成任务"{task}"，我需要到达什么类型的页面？

例如：
- 如果任务是"获取XX列表"，我需要**列表页**
- 如果任务是"查询XX信息"，我需要**搜索页**或**详情页**
- 如果任务是"提交XX"，我需要**表单页**

### Step 3: 差距分析
当前页面类型 vs 需要的页面类型：

- **如果匹配**: 可以开始提取数据或填写表单
- **如果不匹配**: 需要导航到正确的页面

### Step 4: 导航策略（如果需要导航）
观察可用的元素和页面内容，哪个最可能通向目标页面？

推理依据：
1. **页面内容**: 页面上的文本是否提到了目标信息？
2. **文本语义**: 元素的文本是否与任务相关？
   - 例如：任务是"获取热门文章"，那么"最新动态"、"热门推荐"等链接很可能是正确的
3. **元素位置**: 导航栏（nav区域）的链接通常是主要入口
4. **页面结构**: 标题和内容块能否提供线索？

### Step 5: 循环检测
检查是否陷入循环：
- 是否多次访问同一个URL？
- 是否重复点击同一个元素？
- 如果是，说明当前策略无效，需要尝试其他方法

### Step 6: 最终决策
基于以上分析，返回JSON格式的决策：

```json
{{
    "page_type": "homepage",  // 当前页面类型
    "target_page_type": "list_page",  // 需要的页面类型
    "needs_navigation": true,  // 是否需要导航
    "is_stuck": false,  // 是否陷入循环
    "reasoning": "详细的推理过程（2-3句话）",
    "action": {{
        "type": "click",  // click | input | scroll | extract | done | failed
        "target_ref": "lin-2",  // 目标元素的ref
        "value": "",  // 如果是input操作，填写的值
        "confidence": 0.8,  // 信心度 0-1
        "reason": "为什么选择这个动作"
    }}
}}
```

**动作类型说明**：
- `click`: 点击元素（用于导航、按钮等）
- `input`: 在输入框中输入文本
- `scroll`: 滚动页面（如果需要加载更多内容）
- `extract`: 提取当前页面的数据（当到达目标页面时）
- `done`: 任务完成（已经提取到数据）
- `failed`: 任务无法完成（找不到路径或遇到错误）

**重要原则**：
1. 基于观察，不要猜测
2. 充分利用页面内容和结构信息
3. 如果页面内容已经包含目标信息，直接提取
4. 如果发现循环，尝试其他路径
5. 如果连续3次无法前进，考虑返回failed

现在请开始推理并返回决策JSON。
"""

        # 调用LLM进行推理
        response = await self.llm.chat(
            prompt,
            temperature=0.3,
            json_mode=True
        )

        # 解析响应
        try:
            analysis = json.loads(response)
        except json.JSONDecodeError as e:
            log_error(f"LLM返回的JSON格式错误: {response}")
            raise ValueError(f"LLM返回格式错误: {e}")

        # 构建决策对象
        action_data = analysis.get("action", {})

        decision = ActionDecision(
            action_type=action_data.get("type", "failed"),
            target_ref=action_data.get("target_ref", ""),
            value=action_data.get("value", ""),
            reasoning=analysis.get("reasoning", ""),
            page_analysis={
                "page_type": analysis.get("page_type", "unknown"),
                "target_page_type": analysis.get("target_page_type", "unknown"),
                "needs_navigation": analysis.get("needs_navigation", False),
                "is_stuck": analysis.get("is_stuck", False)
            },
            confidence=action_data.get("confidence", 0.5)
        )

        return decision

    async def _execute_action(
        self,
        decision: ActionDecision,
        elements: List[AccessibleElement]
    ) -> Dict[str, Any]:
        """
        执行决策的动作

        Args:
            decision: 决策对象
            elements: 当前页面的元素列表

        Returns:
            执行结果
        """
        action_type = decision.action_type

        if action_type == "click":
            # 通过ref找到元素
            element = self.a11y_extractor.get_element_by_ref(decision.target_ref)
            if not element:
                return {
                    "success": False,
                    "error": f"找不到ref: {decision.target_ref}"
                }

            console.print(f"[cyan]🖱️  点击: [{element.ref}] {element.role} - {element.name[:50]}[/cyan]")

            # 执行点击
            result = await self.toolkit.click(element.selector)
            return {"success": result.success, "error": result.error}

        elif action_type == "input":
            # 输入文本
            element = self.a11y_extractor.get_element_by_ref(decision.target_ref)
            if not element:
                return {
                    "success": False,
                    "error": f"找不到ref: {decision.target_ref}"
                }

            console.print(f"[cyan]⌨️  输入: [{element.ref}] {element.name[:30]} = '{decision.value}'[/cyan]")

            result = await self.toolkit.input_text(element.selector, decision.value)
            return {"success": result.success, "error": result.error}

        elif action_type == "scroll":
            console.print(f"[cyan]📜 滚动页面[/cyan]")
            result = await self.toolkit.scroll_down(500)
            return {"success": result.success, "error": result.error}

        elif action_type == "extract":
            # 提取数据（在主循环中处理）
            return {"success": True}

        else:
            return {
                "success": False,
                "error": f"未知的动作类型: {action_type}"
            }

    async def _extract_data(
        self,
        task: str,
        page_content  # PageContent对象
    ) -> Dict[str, Any]:
        """
        从当前页面提取数据

        使用LLM理解任务需求并提取相关数据
        """
        console.print("[cyan]📊 开始提取数据...[/cyan]")

        # 获取页面文本内容
        text_result = await self.toolkit.get_text("body")
        page_text = text_result.data if text_result.success else ""

        # 截断文本（避免超出上下文）
        if len(page_text) > 10000:
            page_text = page_text[:10000] + "\n... (内容过长，已截断)"

        # 构建提取prompt
        prompt = f"""你需要从当前页面提取数据来完成任务。

## 任务
{task}

## 页面标题
{page_content.title}

## 页面主要标题
{', '.join(page_content.main_headings) if page_content.main_headings else '无'}

## 页面内容摘要
{chr(10).join(page_content.text_blocks[:10]) if page_content.text_blocks else '无内容'}

## 完整页面文本
{page_text}

请提取与任务相关的数据，返回JSON格式：

```json
{{
    "extracted_data": [
        // 提取的数据项列表
        // 例如：{{"title": "...", "link": "...", "date": "..."}}
    ],
    "summary": "提取结果的简短总结",
    "data_type": "list" | "single_item" | "text"
}}
```

如果页面上没有相关数据，返回空列表。
"""

        response = await self.llm.chat(prompt, temperature=0.2, json_mode=True)

        try:
            extraction_result = json.loads(response)
            log_success(f"✓ 提取完成: {extraction_result.get('summary', '')}")
            return extraction_result
        except json.JSONDecodeError:
            log_error("数据提取失败：LLM返回格式错误")
            return {
                "extracted_data": [],
                "summary": "提取失败",
                "error": "JSON解析错误"
            }

    def _build_history_context(self) -> str:
        """构建操作历史的上下文"""
        if not self.action_history:
            return "（这是第一步，还没有历史操作）"

        lines = []
        for record in self.action_history[-5:]:  # 只显示最近5步
            status = "✓" if record["success"] else "✗"
            lines.append(
                f"Step {record['step']}: {status} {record['action']} "
                f"[{record['target_ref']}] - {record['reasoning'][:60]}"
            )

        return "\n".join(lines)

    async def _wait_for_page_ready(self, timeout: int = 5000):
        """等待页面加载完成"""
        try:
            await self.toolkit.page.wait_for_load_state("domcontentloaded", timeout=timeout)
            await asyncio.sleep(0.5)  # 额外等待动态内容
        except Exception as e:
            log_warning(f"等待页面加载超时: {e}")
