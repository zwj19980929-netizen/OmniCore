"""
OmniCore Critic Agent - 独立审查官
在最终结果返回或执行高危操作前进行逻辑校验
"""
from typing import Dict, Any, Optional
from pathlib import Path

from core.statuses import BLOCKED, WAITING_FOR_APPROVAL, WAITING_FOR_EVENT
from core.state import OmniCoreState
from core.llm import LLMClient
from utils.logger import log_agent_action, logger
from utils.prompt_manager import get_prompt


class CriticAgent:
    """
    独立审查官 Agent
    负责结果校验和质量把控
    """

    def __init__(self, llm_client: LLMClient = None):
        self.llm = llm_client or LLMClient()
        self.name = "Critic"
        self.system_prompt = get_prompt("critic_system", "")

    def review_task_result(
        self,
        task_description: str,
        task_result: Any,
        expected_format: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        审查单个任务的执行结果

        Args:
            task_description: 任务描述
            task_result: 任务执行结果
            expected_format: 期望的输出格式

        Returns:
            审查结果字典
        """
        log_agent_action(self.name, "开始审查任务结果", task_description[:50])

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
                review_result = self.review_task_result(
                    task_description=task["description"],
                    task_result=task["result"],
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
