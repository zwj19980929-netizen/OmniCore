"""
增强版 Web Worker - 集成三层感知架构
"""
from typing import Dict, Any, List, Optional
from pathlib import Path

from core.llm import LLMClient
from utils.browser_toolkit import BrowserToolkit
from utils.page_perceiver import get_page_understanding
from utils.logger import log_agent_action, log_success, log_warning, log_error


# 加载新的 prompt
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "page_perception.txt"
try:
    _PROMPTS = _PROMPT_PATH.read_text(encoding="utf-8-sig")
except Exception:
    _PROMPTS = ""

# 解析三个阶段的 prompt
def _extract_prompt(name: str) -> str:
    pattern = f"# {name}\n(.*?)(?=\n# |$)"
    import re
    match = re.search(pattern, _PROMPTS, re.DOTALL)
    return match.group(1).strip() if match else ""

PAGE_UNDERSTANDING_PROMPT = _extract_prompt("第一阶段：页面理解")
SELECTOR_GENERATION_PROMPT = _extract_prompt("第二阶段：选择器生成")
ACTION_PLANNING_PROMPT = _extract_prompt("第三阶段：操作规划")


class EnhancedWebWorker:
    """
    增强版 Web Worker - 三层感知架构

    Layer 1: 页面理解 - 理解页面是什么、有什么功能
    Layer 2: 选择器生成 - 基于理解生成精确的提取策略
    Layer 3: 操作规划 - 规划具体的操作步骤
    """

    def __init__(self, llm_client: Optional[LLMClient] = None):
        self.name = "EnhancedWebWorker"
        self.llm = llm_client or LLMClient()
        self._understanding_cache: Dict[str, Dict[str, Any]] = {}

    async def smart_extract(
        self,
        toolkit: BrowserToolkit,
        task_description: str,
        limit: int = 10
    ) -> Dict[str, Any]:
        """
        智能提取 - 三层感知流程

        Args:
            toolkit: 浏览器工具包
            task_description: 任务描述
            limit: 提取数量限制

        Returns:
            提取结果
        """
        log_agent_action(self.name, "开始三层感知分析", task_description[:50])

        # === Layer 1: 页面理解 ===
        understanding = await self._understand_page(toolkit, task_description)

        if not understanding.get("success"):
            return {
                "success": False,
                "error": "页面理解失败",
                "data": []
            }

        log_success(f"页面理解完成: {understanding.get('page_type')} - {understanding.get('main_function')}")

        # === Layer 2: 选择器生成 ===
        selector_config = await self._generate_selectors(
            toolkit,
            task_description,
            understanding
        )

        if not selector_config.get("success"):
            return {
                "success": False,
                "error": "选择器生成失败",
                "data": []
            }

        log_success(f"选择器生成完成: {selector_config.get('item_selector')}")

        # === Layer 3: 执行提取 ===
        result = await self._execute_extraction(
            toolkit,
            selector_config,
            limit
        )

        return result

    async def _understand_page(
        self,
        toolkit: BrowserToolkit,
        task_description: str
    ) -> Dict[str, Any]:
        """
        Layer 1: 页面理解

        获取页面的结构化表示，让 LLM 理解页面功能
        """
        log_agent_action(self.name, "Layer 1: 页面理解")

        # 获取页面结构化描述
        page_structure = await get_page_understanding(toolkit, task_description)

        # 缓存检查
        url_r = await toolkit.get_current_url()
        cache_key = f"{url_r.data}:{task_description}"
        if cache_key in self._understanding_cache:
            log_agent_action(self.name, "命中页面理解缓存")
            return self._understanding_cache[cache_key]

        # LLM 分析页面
        prompt = PAGE_UNDERSTANDING_PROMPT.format(
            task_description=task_description,
            page_structure=page_structure
        )

        response = self.llm.chat_with_system(
            system_prompt=prompt,
            user_message="请分析这个页面并返回 JSON 格式的理解结果",
            temperature=0.2,
            max_tokens=2048,
            json_mode=True
        )

        try:
            understanding = self.llm.parse_json_response(response)
            understanding["success"] = True
            understanding["page_structure"] = page_structure  # 保存供下一层使用

            # 缓存结果
            self._understanding_cache[cache_key] = understanding

            return understanding
        except Exception as e:
            log_error(f"页面理解解析失败: {e}")
            return {"success": False, "error": str(e)}

    async def _generate_selectors(
        self,
        toolkit: BrowserToolkit,
        task_description: str,
        understanding: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Layer 2: 选择器生成

        基于页面理解，生成精确的数据提取选择器
        """
        log_agent_action(self.name, "Layer 2: 选择器生成")

        # 构建 prompt
        prompt = SELECTOR_GENERATION_PROMPT.format(
            task_description=task_description,
            page_understanding=str(understanding),
            page_structure=understanding.get("page_structure", "")
        )

        response = self.llm.chat_with_system(
            system_prompt=prompt,
            user_message="请生成数据提取选择器配置",
            temperature=0.1,
            max_tokens=3072,
            json_mode=True
        )

        try:
            config = self.llm.parse_json_response(response)
            return config
        except Exception as e:
            log_error(f"选择器生成解析失败: {e}")
            return {"success": False, "error": str(e)}

    async def _execute_extraction(
        self,
        toolkit: BrowserToolkit,
        config: Dict[str, Any],
        limit: int
    ) -> Dict[str, Any]:
        """
        Layer 3: 执行提取

        根据选择器配置执行数据提取
        """
        log_agent_action(self.name, "Layer 3: 执行数据提取")

        # 执行预操作
        pre_actions = config.get("pre_actions", [])
        for action in pre_actions:
            if action.get("action") == "click":
                selector = action.get("selector")
                log_agent_action(self.name, f"执行预操作: 点击 {selector}")
                await toolkit.click(selector)
                await toolkit.human_delay(1000, 2000)

        # 提取数据
        item_selector = config.get("item_selector", "")
        fields = config.get("fields", {})

        if not item_selector:
            return {
                "success": False,
                "error": "未找到有效的 item_selector",
                "data": []
            }

        results = []

        try:
            # 查询所有数据项
            items_r = await toolkit.query_all(item_selector)
            if not items_r.success:
                return {
                    "success": False,
                    "error": f"查询失败: {items_r.error}",
                    "data": []
                }

            items = items_r.data or []
            log_agent_action(self.name, f"找到 {len(items)} 个数据项")

            # 提取每个数据项的字段
            for i, item in enumerate(items[:limit]):
                data = {"index": i + 1}

                for field_name, selector in fields.items():
                    if not selector:
                        continue

                    try:
                        elem = await item.query_selector(selector)
                        if elem:
                            # 获取文本
                            text = (await elem.inner_text()).strip()
                            data[field_name] = text

                            # 如果是链接，提取 href
                            tag = await elem.evaluate("el => el.tagName.toLowerCase()")
                            if tag == "a":
                                href = await elem.get_attribute("href")
                                if href:
                                    # 转换为绝对 URL
                                    url_r = await toolkit.get_current_url()
                                    from urllib.parse import urljoin
                                    absolute_url = urljoin(url_r.data or "", href)
                                    data[f"{field_name}_url"] = absolute_url
                    except Exception as e:
                        log_warning(f"提取字段 {field_name} 失败: {e}")

                # 只保留有内容的数据项
                if len([v for k, v in data.items() if k != "index" and v]) > 0:
                    results.append(data)

            log_success(f"成功提取 {len(results)} 条数据")

            return {
                "success": True,
                "data": results,
                "count": len(results),
                "config": config
            }

        except Exception as e:
            log_error(f"数据提取失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "data": []
            }

    async def plan_next_action(
        self,
        toolkit: BrowserToolkit,
        task_description: str,
        understanding: Dict[str, Any],
        collected_count: int,
        target_count: int
    ) -> Dict[str, Any]:
        """
        规划下一步操作

        根据当前状态和页面理解，决定下一步应该做什么
        """
        url_r = await toolkit.get_current_url()
        title_r = await toolkit.get_title()

        prompt = ACTION_PLANNING_PROMPT.format(
            task_description=task_description,
            page_understanding=str(understanding),
            current_url=url_r.data or "",
            page_title=title_r.data or "",
            collected_count=collected_count,
            target_count=target_count
        )

        response = self.llm.chat_with_system(
            system_prompt=prompt,
            user_message="请规划下一步操作",
            temperature=0.3,
            max_tokens=1536,
            json_mode=True
        )

        try:
            plan = self.llm.parse_json_response(response)
            return plan
        except Exception as e:
            log_error(f"操作规划解析失败: {e}")
            return {
                "next_action": "done",
                "reasoning": f"规划失败: {e}"
            }
