"""
OmniCore Critic Agent - 独立审查官
在最终结果返回或执行高危操作前进行逻辑校验
"""
import re
from datetime import datetime
from typing import Dict, Any, Optional
from pathlib import Path

from core.statuses import BLOCKED, WAITING_FOR_APPROVAL, WAITING_FOR_EVENT
from core.state import OmniCoreState
from core.llm import LLMClient
from core.prompt_registry import build_single_section_prompt
from utils.logger import log_agent_action, logger
from utils.prompt_manager import get_prompt
from utils.time_context import is_time_sensitive_text, normalize_current_time_context
from utils.url_utils import extract_all_urls
from utils.structured_extract import extract_requested_item_count
from utils.web_result_normalizer import (
    best_title_from_item,
    best_url_from_item,
    looks_like_detail_list_item,
    normalize_text,
    tokenize_text,
)


class CriticAgent:
    """
    独立审查官 Agent
    负责结果校验和质量把控
    """

    def __init__(self, llm_client: LLMClient = None):
        self.llm = llm_client or LLMClient()
        self.name = "Critic"
        raw = get_prompt("critic_system", "")
        self.system_prompt = build_single_section_prompt("critic_system", raw)

    @staticmethod
    def _task_has_direct_url(task_description: str) -> bool:
        return bool(re.search(r"https?://\S+", str(task_description or "")))

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
        return extract_requested_item_count(task_description)

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

    _GENERIC_RELEVANCE_TOKENS = {
        "a", "an", "and", "at", "browser", "current", "data", "extract",
        "for", "from", "go", "google", "latest", "link", "links", "list",
        "news", "of", "open", "page", "query", "recent", "result",
        "results", "search", "source", "sources", "the", "title", "titles",
        "to", "url", "urls", "use", "visit", "web",
        "baidu", "bing", "duckduckgo", "google新闻", "谷歌", "百度", "必应",
        "使用", "访问", "打开", "搜索", "结果", "标题", "链接", "新闻",
        "最新", "最新动态", "当前", "最近", "提取", "抓取", "条", "个",
    }

    @classmethod
    def _task_relevance_tokens(cls, task_description: str) -> list[str]:
        text = re.sub(r"https?://\S+", " ", str(task_description or "")).lower()
        tokens = []
        for token in tokenize_text(text):
            normalized = normalize_text(token).lower()
            if not normalized or normalized in cls._GENERIC_RELEVANCE_TOKENS:
                continue
            if normalized.isdigit():
                continue
            if len(normalized) <= 1:
                continue
            tokens.append(normalized)
        return list(dict.fromkeys(tokens))[:12]

    @staticmethod
    def _token_in_text(token: str, text: str) -> bool:
        if re.fullmatch(r"[a-z0-9.+-]{1,3}", token):
            return bool(re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text))
        return token in text

    @classmethod
    def _result_has_task_relevant_items(cls, task_description: str, task_result: Any) -> bool:
        if not isinstance(task_result, dict):
            return False
        data = task_result.get("data")
        if not isinstance(data, list) or not data:
            return False
        tokens = cls._task_relevance_tokens(task_description)
        if not tokens:
            return True

        relevant_count = 0
        for item in data[:12]:
            if not isinstance(item, dict):
                continue
            haystack = " ".join(
                normalize_text(value).lower()
                for value in (
                    best_title_from_item(item),
                    item.get("summary"),
                    item.get("snippet"),
                    item.get("text"),
                    item.get("source"),
                    best_url_from_item(item),
                )
                if normalize_text(value)
            )
            if any(cls._token_in_text(token, haystack) for token in tokens):
                relevant_count += 1

        target_count = cls._extract_target_count(task_description)
        required_relevant = min(3, max(1, (target_count or len(data)) // 2))
        return relevant_count >= required_relevant

    def _deterministic_review_result(
        self,
        task_description: str,
        task_result: Any,
        current_time_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(task_result, dict):
            return None
        if not bool(task_result.get("success", False)):
            return None
        if str(task_result.get("message", "") or "").lower().find("blocked page") >= 0:
            return None

        report_review = self._review_generated_report_sources(
            task_description,
            task_result,
            current_time_context=current_time_context,
        )
        if report_review is not None:
            return report_review

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
            and not self._result_has_task_relevant_items(task_description, task_result)
        ):
            return {
                "approved": False,
                "score": 0.35,
                "issues": ["返回条目与搜索主题不匹配，更像首页导航、热榜或无关链接"],
                "suggestions": ["先提交搜索词并等待结果页，再提取与查询主题匹配的标题、链接和摘要"],
                "summary": "列表抽取结果与任务主题不匹配",
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

    @staticmethod
    def _looks_like_source_sensitive_report_task(task_description: str) -> bool:
        lowered = str(task_description or "").lower()
        report_tokens = ("报告", "总结", "summary", "report", "summarize", "生成")
        source_tokens = ("来源", "信息源", "source", "sources", "citation", "引用", "链接")
        latest_tokens = ("最新", "latest", "recent", "up-to-date")
        return any(token in lowered for token in report_tokens) and (
            any(token in lowered for token in source_tokens)
            or any(token in lowered for token in latest_tokens)
            or is_time_sensitive_text(lowered)
        )

    def _review_generated_report_sources(
        self,
        task_description: str,
        task_result: Dict[str, Any],
        current_time_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self._looks_like_source_sensitive_report_task(task_description):
            return None
        file_path = str(task_result.get("file_path", "") or "").strip()
        if not file_path:
            return None

        path = Path(file_path).expanduser()
        if not path.exists() or not path.is_file():
            return {
                "approved": False,
                "score": 0.25,
                "issues": [f"报告文件不存在，无法验证来源: {file_path}"],
                "suggestions": ["确认 file_worker 返回的是实际写入路径"],
                "summary": "报告文件路径不可验证",
            }

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return {
                "approved": False,
                "score": 0.25,
                "issues": [f"无法读取报告文件进行来源验证: {exc}"],
                "suggestions": ["检查文件编码和权限"],
                "summary": "报告文件读取失败",
            }

        http_urls = [url for url in extract_all_urls(content) if url.startswith(("http://", "https://"))]
        if not http_urls:
            return {
                "approved": False,
                "score": 0.35,
                "issues": ["报告要求包含信息源，但正文没有可点击的 HTTP/HTTPS 来源链接"],
                "suggestions": ["基于上游抓取结果重新生成报告，并在文末列出每条来源 URL"],
                "summary": "报告缺少可验证来源链接",
            }

        time_context = normalize_current_time_context(current_time_context)
        current_year = int(time_context.get("current_year") or datetime.now().year)
        local_date = str(time_context.get("local_date", "") or "").strip()
        latest_task = is_time_sensitive_text(task_description)
        dated_report = re.search(
            r"(?:报告生成日期|生成日期|generated(?:\s+at|\s+on)?)[^\d]{0,20}(20\d{2})",
            content,
            flags=re.IGNORECASE,
        )
        if dated_report:
            try:
                report_year = int(dated_report.group(1))
            except ValueError:
                report_year = current_year
            if report_year < current_year and latest_task:
                return {
                    "approved": False,
                    "score": 0.4,
                    "issues": [f"报告生成日期为 {report_year}，与最新资料任务的当前年份 {current_year} 不符"],
                    "suggestions": ["重新联网检索并生成当前日期的报告"],
                    "summary": "报告明显过期",
                }
        elif latest_task:
            return {
                "approved": False,
                "score": 0.45,
                "issues": ["时间敏感报告缺少明确生成日期，无法确认“最新/当前”的时间基准"],
                "suggestions": ["重新生成报告，并写明当前运行日期和来源发布日期"],
                "summary": "报告缺少当前日期基准",
            }

        stale_claim_patterns = (
            r"(20\d{2})\s*年(?:初|上半年|下半年|截至|以来)?[^。.\n]{0,30}(?:最新|当前|目前)",
            r"(?:最新|当前|目前|latest|current)[^。.\n]{0,30}(20\d{2})\s*年",
        )
        if latest_task:
            for pattern in stale_claim_patterns:
                for match in re.finditer(pattern, content, flags=re.IGNORECASE):
                    try:
                        claim_year = int(match.group(1))
                    except Exception:
                        continue
                    if claim_year < current_year:
                        return {
                            "approved": False,
                            "score": 0.4,
                            "issues": [f"报告将 {claim_year} 年信息表述为最新/当前，但当前年份是 {current_year}"],
                            "suggestions": ["基于当前日期重新检索，或明确说明旧年份信息为何仍是最新"],
                            "summary": "报告存在过期的最新性表述",
                        }

            if local_date and local_date not in content and str(current_year) not in content:
                return {
                    "approved": False,
                    "score": 0.5,
                    "issues": [f"时间敏感报告未体现当前日期或年份（{local_date}）"],
                    "suggestions": ["在报告中加入生成日期，并核对当前年份来源"],
                    "summary": "报告缺少当前时间标记",
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
        current_time_context: Optional[Dict[str, Any]] = None,
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

        # Quick check: success=True but data is empty/missing → reject
        if (
            isinstance(task_result, dict)
            and task_result.get("success")
            and self._looks_like_list_extraction_task(task_description)
        ):
            data = task_result.get("data")
            if not data or (isinstance(data, list) and len(data) == 0):
                return {
                    "approved": False,
                    "score": 0.3,
                    "issues": ["success=True 但 data 为空，任务声称成功但未提取到任何数据"],
                    "suggestions": ["检查提取逻辑是否正确识别了页面结构"],
                    "summary": "任务标记成功但无数据返回",
                }

        deterministic_result = self._deterministic_review_result(
            task_description,
            task_result,
            current_time_context=current_time_context,
        )
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

        current_time_context = None
        try:
            from core.message_bus import MessageBus, MSG_TIME_CONTEXT

            bus = MessageBus.from_dict(state.get("message_bus", []))
            time_msg = bus.get_latest(MSG_TIME_CONTEXT)
            if time_msg:
                current_time_context = time_msg.payload.get("value")
        except Exception:
            current_time_context = None

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

            if task.get("skipped_by_adaptive_reroute") or (
                isinstance(task.get("result"), dict)
                and task["result"].get("skipped_by_adaptive_reroute")
            ):
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
                    current_time_context=current_time_context,
                )
                task["critic_review"] = review_result
                task["critic_approved"] = bool(review_result.get("approved"))
                task["critic_score"] = float(review_result.get("score", 0.0) or 0.0)

                if not review_result.get("approved"):
                    all_approved = False
                    task["failure_source"] = "critic"
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
