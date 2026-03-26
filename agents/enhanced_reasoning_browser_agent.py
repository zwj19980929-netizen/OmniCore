"""
增强版推理式浏览器Agent - 集成经验记忆

在基础推理Agent上增加经验学习能力
"""

import asyncio
import json
from typing import Dict, List, Any, Optional
from urllib.parse import urlparse

from core.llm import LLMClient
from utils.browser_toolkit import BrowserToolkit
from utils.accessibility_tree_extractor import (
    AccessibilityTreeExtractor,
    AccessibleElement
)
from utils.experience_memory import ExperienceMemory
from utils.logger import log_error, log_success, log_warning, console
from agents.reasoning_browser_agent import ReasoningBrowserAgent, ActionDecision


class EnhancedReasoningBrowserAgent(ReasoningBrowserAgent):
    """
    增强版推理式浏览器Agent

    在基础推理能力上增加：
    1. 经验记忆：从成功/失败中学习
    2. 经验提示：为新任务提供相似经验
    3. 失败避免：避免重复失败的策略
    """

    def __init__(
        self,
        llm_client: LLMClient,
        toolkit: BrowserToolkit,
        experience_storage_path: str = "data/agent_experiences.json"
    ):
        super().__init__(llm_client, toolkit)
        self.experience_memory = ExperienceMemory(experience_storage_path)

    async def run(self, task: str, start_url: str, max_steps: int = 10) -> Dict[str, Any]:
        """
        执行任务（带经验学习）

        Args:
            task: 任务描述
            start_url: 起始URL
            max_steps: 最大步数

        Returns:
            执行结果
        """
        console.print(f"[magenta]🧠 增强版推理Agent启动（带经验学习）[/magenta]")
        console.print(f"[cyan]任务: {task}[/cyan]")
        console.print(f"[cyan]起始URL: {start_url}[/cyan]")

        # 1. 查找相似经验
        domain = urlparse(start_url).netloc
        experience_hint = self.experience_memory.find_similar_experience(task, start_url)

        if experience_hint:
            log_success("✓ 找到相似的成功经验！")
            print(experience_hint)
        else:
            console.print("[cyan]ℹ️ 没有找到相似经验，将从零开始探索[/cyan]")

        # 获取域名统计
        domain_stats = self.experience_memory.get_domain_statistics(domain)
        if domain_stats["total"] > 0:
            console.print(
                f"[cyan]📊 {domain} 历史统计: "
                f"成功率 {domain_stats['success_rate']:.0%} "
                f"({domain_stats['successful']}/{domain_stats['total']})[/cyan]"
            )

        # 获取失败模式（避免重复）
        failed_patterns = self.experience_memory.get_failed_patterns(domain)
        if failed_patterns:
            log_warning(f"⚠️ 该域名上失败过的模式: {', '.join(failed_patterns)}")

        # 2. 执行任务（调用父类方法）
        result = await super().run(task, start_url, max_steps)

        # 3. 保存经验
        self.experience_memory.save_experience(
            task=task,
            url=start_url,
            action_history=self.action_history,
            result=result
        )

        # 4. 如果成功，显示学习总结
        if result.get("success"):
            log_success("✅ 任务成功！经验已保存，下次遇到类似任务会更快。")
        else:
            log_warning("❌ 任务失败，但失败经验也很有价值，已记录。")

        return result

    async def _analyze_and_decide(
        self,
        task: str,
        page_content,  # PageContent对象
        current_url: str,
        step: int
    ) -> ActionDecision:
        """
        增强版推理：加入经验提示

        覆盖父类方法，在推理时加入经验提示
        """

        # 构建完整的页面上下文
        full_context = self.page_perceiver.to_llm_context(page_content)

        # 构建历史上下文
        history_context = self._build_history_context()

        # 获取经验提示
        experience_hint = self.experience_memory.find_similar_experience(
            task, current_url
        )

        # 获取失败模式
        domain = urlparse(current_url).netloc
        failed_patterns = self.experience_memory.get_failed_patterns(domain)

        # 构建增强的prompt
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
"""

        # 添加经验提示（如果有）
        if experience_hint:
            prompt += f"""

## 💡 相似任务的成功经验
{experience_hint}

注意：这是参考经验，当前页面可能不同，请根据实际情况调整。
"""

        # 添加失败模式警告（如果有）
        if failed_patterns:
            prompt += f"""

## ⚠️ 避免重复失败
该网站上以下模式曾经失败过，请尽量避免：
{', '.join(failed_patterns)}
"""

        prompt += """

---

请按照以下步骤进行**逐步推理**（Chain-of-Thought）：

### Step 1: 页面类型识别
观察页面上的元素，判断这是什么类型的页面？

可能的类型：
- **首页/门户页**: 有导航菜单、分类链接，但没有具体的数据列表
- **列表页**: 有多条相似的数据项（如新闻列表、产品列表、搜索结果）
- **详情页**: 单个项目的详细信息
- **搜索页**: 有搜索框和搜索按钮
- **搜索结果页**: 显示搜索结果列表
- **表单页**: 有多个输入框和提交按钮
- **错误页**: 404、403等错误页面

### Step 2: 任务需求分析
要完成任务"{task}"，我需要到达什么类型的页面？

### Step 3: 差距分析
当前页面类型 vs 需要的页面类型：
- **如果匹配**: 可以开始提取数据
- **如果不匹配**: 需要导航

### Step 4: 导航策略（如果需要导航）
观察可用的元素，哪个最可能通向目标页面？

推理依据：
1. **文本语义**: 元素的文本是否与任务相关？
2. **元素位置**: 导航栏（nav区域）的链接通常是主要入口
3. **元素类型**: link通常用于导航，button通常用于操作
4. **经验参考**: 如果有相似经验，可以参考（但要验证当前页面是否适用）

### Step 5: 循环检测
检查是否陷入循环：
- 是否多次访问同一个URL？
- 是否重复点击同一个元素？
- 是否在使用已知会失败的模式？

### Step 6: 最终决策
返回JSON格式的决策：

```json
{
    "page_type": "homepage",
    "target_page_type": "list_page",
    "needs_navigation": true,
    "is_stuck": false,
    "reasoning": "详细的推理过程（2-3句话）",
    "action": {
        "type": "click",  // click | input | scroll | extract | done | failed
        "target_ref": "lin-2",
        "value": "",
        "confidence": 0.8,
        "reason": "为什么选择这个动作"
    }
}
```

**重要原则**：
1. 基于观察，不要猜测
2. 参考经验但不盲从，要验证当前页面
3. 避免已知的失败模式
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


# 便捷函数：创建增强版Agent
def create_enhanced_agent(
    llm_client: LLMClient,
    toolkit: BrowserToolkit
) -> EnhancedReasoningBrowserAgent:
    """
    创建增强版推理Agent的便捷函数

    Args:
        llm_client: LLM客户端
        toolkit: 浏览器工具包

    Returns:
        增强版Agent实例
    """
    return EnhancedReasoningBrowserAgent(llm_client, toolkit)
