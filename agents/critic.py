"""
OmniCore Critic Agent - 独立审查官
在最终结果返回或执行高危操作前进行逻辑校验
"""
import re
from typing import Dict, Any, Optional
from pathlib import Path

from core.statuses import BLOCKED, WAITING_FOR_APPROVAL, WAITING_FOR_EVENT
from core.state import OmniCoreState
from core.llm import LLMClient
from utils.logger import log_agent_action, logger
from utils.prompt_manager import get_prompt
from utils.url_utils import extract_all_urls
from utils.web_result_normalizer import looks_like_detail_list_item
from config.domain_keywords import WEATHER_KEYWORDS_TUPLE, WEATHER_CONDITIONS_TUPLE


class CriticAgent:
    """
    独立审查官 Agent
    负责结果校验和质量把控
    """

    def __init__(self, llm_client: LLMClient = None):
        self.llm = llm_client or LLMClient()
        self.name = "Critic"
        self.system_prompt = get_prompt("critic_system", "")

    @staticmethod
    def _task_has_direct_url(task_description: str) -> bool:
        return bool(re.search(r"https?://\S+", str(task_description or "")))

    @staticmethod
    def _looks_like_weather_task(task_description: str) -> bool:
        lowered = str(task_description or "").lower()
        return any(token in lowered for token in WEATHER_KEYWORDS_TUPLE)

    @staticmethod
    def _looks_like_list_extraction_task(task_description: str) -> bool:
        lowered = str(task_description or "").lower()
        list_tokens = (
            "前",
            "top",
            "title",
            "titles",
            "link",
            "links",
            "headline",
            "headlines",
            "列表",
            "标题",
            "链接",
            "仓库",
            "新闻",
            "抓取前",
        )
        return any(token in lowered for token in list_tokens) or bool(
            re.search(r"\b\d+\s*(?:items?|results?)\b", lowered)
        )

    @staticmethod
    def _extract_target_count(task_description: str) -> int:
        text = str(task_description or "")
        patterns = (
            r"前\s*(\d+)\s*(?:条|个|项|篇)?",
            r"top\s*(\d+)",
            r"(\d+)\s*(?:items?|results?|links?|headlines?|stories|repositories|repos)\b",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            try:
                value = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return 0

    @staticmethod
    def _primary_task_url(task_description: str) -> str:
        urls = extract_all_urls(task_description)
        if urls:
            return str(urls[0] or "").strip()
        return ""

    @classmethod
    def _result_has_title_link_items(cls, task_description: str, task_result: Any) -> bool:
        if not isinstance(task_result, dict):
            return False
        data = task_result.get("data")
        if not isinstance(data, list) or not data:
            return False
        reference_url = cls._primary_task_url(task_description)
        meaningful = 0
        for item in data[:8]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("name") or "").strip()
            link = str(item.get("link", item.get("url", "")) or "").strip()
            if title and link and looks_like_detail_list_item({"title": title, "url": link}, reference_url=reference_url):
                meaningful += 1
        return meaningful > 0

    @classmethod
    def _result_meets_target_count(cls, task_description: str, task_result: Any) -> bool:
        if not isinstance(task_result, dict):
            return False
        target_count = cls._extract_target_count(task_description)
        if target_count <= 0:
            return True
        data = task_result.get("data")
        if not isinstance(data, list):
            return False
        return len(data) >= target_count

    @staticmethod
    def _result_has_weather_signals(task_result: Any) -> bool:
        if not isinstance(task_result, dict):
            return False
        data = task_result.get("data")
        if not isinstance(data, list) or not data:
            return False

        categories = set()
        for item in data[:10]:
            haystacks = []
            if isinstance(item, dict):
                haystacks.extend(str(value or "") for value in item.values())
                keys = " ".join(str(key or "") for key in item.keys())
                haystacks.append(keys)
            else:
                haystacks.append(str(item or ""))
            for value in haystacks:
                lowered = value.lower()
                if any(token in lowered for token in ("temperature", "气温", "℃", "°c")):
                    categories.add("temperature")
                if any(token in lowered for token in ("humidity", "湿度")):
                    categories.add("humidity")
                if any(token in lowered for token in ("wind", "风力", "风向")):
                    categories.add("wind")
                if any(token in lowered for token in ("aqi", "air quality", "空气质量")):
                    categories.add("aqi")
                if any(token in lowered for token in WEATHER_KEYWORDS_TUPLE):
                    categories.add("condition")
            if len(categories) >= 2:
                return True
        return False

    def _deterministic_review_result(
        self,
        task_description: str,
        task_result: Any,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(task_result, dict):
            return None
        if not bool(task_result.get("success", False)):
            return None
        if str(task_result.get("message", "") or "").lower().find("blocked page") >= 0:
            return None

        if self._looks_like_weather_task(task_description) and self._result_has_weather_signals(task_result):
            return {
                "approved": True,
                "score": 0.95,
                "issues": [],
                "suggestions": [],
                "summary": "天气任务已提取到足够的关键字段信号",
            }

        if (
            self._task_has_direct_url(task_description)
            and self._looks_like_list_extraction_task(task_description)
            and not self._result_meets_target_count(task_description, task_result)
        ):
            target_count = self._extract_target_count(task_description)
            actual_count = len(task_result.get("data") or []) if isinstance(task_result.get("data"), list) else 0
            return {
                "approved": False,
                "score": 0.35,
                "issues": [f"返回数量不足：期望至少 {target_count} 条，实际仅 {actual_count} 条"],
                "suggestions": ["继续翻页、点击 more/next，或改用可跨页的提取路径"],
                "summary": "显式 URL 列表抽取任务数量未达标",
            }

        if (
            self._task_has_direct_url(task_description)
            and self._looks_like_list_extraction_task(task_description)
            and self._result_meets_target_count(task_description, task_result)
            and not self._result_has_title_link_items(task_description, task_result)
        ):
            return {
                "approved": False,
                "score": 0.4,
                "issues": ["返回的数据更像导航、筛选或翻页链接，而不是目标实体详情链接"],
                "suggestions": ["改用更强的列表区域识别，或排除同页筛选/分页链接"],
                "summary": "显式 URL 列表抽取任务提取到了错误的重复区域",
            }

        if (
            self._task_has_direct_url(task_description)
            and self._looks_like_list_extraction_task(task_description)
            and self._result_meets_target_count(task_description, task_result)
            and self._result_has_title_link_items(task_description, task_result)
        ):
            return {
                "approved": True,
                "score": 0.95,
                "issues": [],
                "suggestions": [],
                "summary": "显式 URL 列表抽取任务已返回有效的标题和链接",
            }

        return None

    def _vision_verify_result(
        self,
        task_description: str,
        task_result: Any,
        page_screenshot: bytes,
    ) -> Optional[Dict[str, Any]]:
        """
        视觉验证：当 LLM 审查给低分时，用截图让视觉模型复核。
        如果截图显示任务实际已完成，则覆盖评分。
        """
        try:
            vision_llm = LLMClient.for_vision()
        except Exception:
            return None

        # 构建简洁的结果摘要（避免把大量数据塞进 prompt）
        result_summary = {}
        if isinstance(task_result, dict):
            result_summary = {
                "success": task_result.get("success"),
                "message": str(task_result.get("message", ""))[:200],
                "url": str(task_result.get("url", ""))[:200],
                "title": str(task_result.get("title", ""))[:200],
                "data_count": len(task_result.get("data") or []),
            }

        prompt = (
            f"You are verifying whether a browser automation task was completed successfully.\n\n"
            f"Task: {task_description}\n\n"
            f"System report: {result_summary}\n\n"
            f"The system thinks this task may have FAILED. Look at the final page screenshot above.\n"
            f"Does the page visually contain the information the task was trying to extract or achieve?\n\n"
            f"Return JSON:\n"
            f'{{"task_completed": true/false, "confidence": 0.0-1.0, '
            f'"extracted_answer": "brief summary of what the page actually shows relevant to the task"}}'
        )

        try:
            response = vision_llm.chat_with_image(prompt, page_screenshot, 0.2, 800)
            parsed = vision_llm.parse_json_response(response)
            completed = bool(parsed.get("task_completed", False))
            confidence = float(parsed.get("confidence", 0.0))
            answer = str(parsed.get("extracted_answer", ""))

            if completed and confidence >= 0.6:
                log_agent_action(
                    self.name,
                    f"视觉复核通过: {answer[:80]}",
                    f"confidence={confidence:.2f}",
                )
                return {
                    "approved": True,
                    "score": min(0.85, confidence),
                    "issues": [],
                    "suggestions": [],
                    "summary": f"视觉复核通过: {answer[:120]}",
                }
            return None
        except Exception as exc:
            logger.warning(f"Critic vision verify failed: {exc}")
            return None

    def review_task_result(
        self,
        task_description: str,
        task_result: Any,
        expected_format: Optional[str] = None,
        page_screenshot: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        """
        审查单个任务的执行结果

        Args:
            task_description: 任务描述
            task_result: 任务执行结果
            expected_format: 期望的输出格式
            page_screenshot: 最终页面截图（可选），用于视觉复核

        Returns:
            审查结果字典
        """
        log_agent_action(self.name, "开始审查任务结果", task_description[:50])

        deterministic_result = self._deterministic_review_result(task_description, task_result)
        if deterministic_result is not None:
            log_agent_action(
                self.name,
                f"审查完成: {'通过' if deterministic_result.get('approved') else '未通过'}",
                f"评分: {deterministic_result.get('score', 0):.2f} (deterministic)",
            )
            return deterministic_result

        review_prompt = f"""请审查以下任务的执行结果：

## 任务描述：
{task_description}

## 执行结果：
{task_result}

## 期望格式：
{expected_format or "无特定要求"}

请给出你的审查意见。"""

        response = self.llm.chat_with_system(
            system_prompt=self.system_prompt,
            user_message=review_prompt,
            temperature=0.2,
            json_mode=True,
        )

        try:
            result = self.llm.parse_json_response(response)
            log_agent_action(
                self.name,
                f"审查完成: {'通过' if result.get('approved') else '未通过'}",
                f"评分: {result.get('score', 0):.2f}"
            )

            # Layer 3: 视觉复核 — 当 LLM 审查给低分且有截图时，用视觉模型复核
            if (
                result.get("score", 0) < 0.5
                and page_screenshot
                and isinstance(task_result, dict)
                and task_result.get("success")
            ):
                log_agent_action(self.name, "LLM 审查低分，启动视觉复核")
                vision_override = self._vision_verify_result(
                    task_description, task_result, page_screenshot,
                )
                if vision_override is not None:
                    return vision_override

            return result
        except Exception as e:
            logger.error(f"Critic 解析失败: {e}")
            return {
                "approved": False,
                "score": 0.0,
                "issues": [f"审查过程出错: {str(e)}"],
                "suggestions": [],
                "summary": "审查失败",
            }

    def verify_file_created(
        self,
        file_path: str,
        min_content_length: int = 10,
    ) -> Dict[str, Any]:
        """
        验证文件是否成功创建且内容有效

        Args:
            file_path: 文件路径
            min_content_length: 最小内容长度

        Returns:
            验证结果
        """
        log_agent_action(self.name, "验证文件创建", file_path)

        path = Path(file_path).expanduser()

        if not path.exists():
            return {
                "approved": False,
                "score": 0.0,
                "issues": [f"文件不存在: {file_path}"],
                "suggestions": ["检查文件路径是否正确", "确认写入操作是否执行"],
                "summary": "文件验证失败 - 文件不存在",
            }

        try:
            content = path.read_text(encoding="utf-8")

            if len(content.strip()) < min_content_length:
                return {
                    "approved": False,
                    "score": 0.3,
                    "issues": [f"文件内容过短: {len(content)} 字符"],
                    "suggestions": ["检查数据抓取是否成功"],
                    "summary": "文件验证失败 - 内容不足",
                }

            return {
                "approved": True,
                "score": 1.0,
                "issues": [],
                "suggestions": [],
                "summary": f"文件验证通过，内容长度: {len(content)} 字符",
                "content_preview": content[:200],
            }

        except Exception as e:
            return {
                "approved": False,
                "score": 0.0,
                "issues": [f"读取文件失败: {str(e)}"],
                "suggestions": ["检查文件编码"],
                "summary": "文件验证失败 - 读取错误",
            }

    def review(self, state: OmniCoreState) -> OmniCoreState:
        """
        LangGraph 节点函数：执行审查逻辑

        Args:
            state: 当前图状态

        Returns:
            更新后的状态
        """
        log_agent_action(self.name, "开始全局审查")

        all_approved = True
        all_issues = []
        all_suggestions = []
        completed_count = 0
        failed_count = 0
        unfinished_count = 0

        # 审查所有已完成的任务，并对失败/未完成任务保持否决。
        for task in state["task_queue"]:
            status = str(task.get("status", "") or "")
            if status in {WAITING_FOR_APPROVAL, WAITING_FOR_EVENT, BLOCKED}:
                continue
            if status == "failed":
                task["critic_approved"] = False
                task["critic_review"] = {
                    "approved": False,
                    "score": 0.0,
                    "issues": [f"任务失败: {task.get('description', task.get('task_id', 'unknown'))}"],
                    "suggestions": [],
                    "summary": "task failed before critic review",
                }
                failed_count += 1
                all_approved = False
                all_issues.append(
                    f"任务失败: {task.get('description', task.get('task_id', 'unknown'))}"
                )
                continue
            if status != "completed":
                task["critic_approved"] = False
                unfinished_count += 1
                all_approved = False
                continue

            completed_count += 1
            if task["result"]:
                # 提取截图供视觉复核，之后清理避免大数据留在 state 中
                _screenshot = None
                if isinstance(task["result"], dict):
                    _screenshot = task["result"].pop("_page_screenshot", None)
                review_result = self.review_task_result(
                    task_description=task["description"],
                    task_result=task["result"],
                    page_screenshot=_screenshot,
                )
                task["critic_review"] = review_result
                task["critic_approved"] = bool(review_result.get("approved"))
                task["critic_score"] = float(review_result.get("score", 0.0) or 0.0)

                if not review_result.get("approved"):
                    all_approved = False
                    all_issues.extend(review_result.get("issues", []))
                    all_suggestions.extend(review_result.get("suggestions", []))

        if state.get("task_queue") and completed_count == 0 and failed_count > 0:
            all_approved = False
            all_issues.append("没有任何任务成功完成")
        if failed_count > 0:
            all_issues.append(f"失败任务数: {failed_count}")
        if unfinished_count > 0:
            all_issues.append(f"未完成任务数: {unfinished_count}")

        # 更新状态
        state["critic_approved"] = all_approved
        state["critic_feedback"] = (
            "所有任务审查通过" if all_approved
            else f"发现问题: {'; '.join(all_issues)}"
        )
        state["execution_status"] = "completed" if all_approved else "reviewing"

        from langchain_core.messages import SystemMessage
        state["messages"].append(
            SystemMessage(content=f"Critic 审查结果: {state['critic_feedback']}")
        )

        return state
