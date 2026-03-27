"""
OmniCore Validator — 纯 Python 硬规则验证层
零 LLM 调用，确定性检查。在 Critic（LLM 审查）之前运行。
"""
from typing import Dict, Any, List
from pathlib import Path
from urllib.parse import urlparse

from core.statuses import BLOCKED, WAITING_FOR_APPROVAL, WAITING_FOR_EVENT
from core.state import OmniCoreState
from utils.logger import log_agent_action, log_success, log_warning


class Validator:
    """确定性硬规则验证器"""

    def __init__(self):
        self.name = "Validator"

    @staticmethod
    def _urls_look_related(expected_url: str, current_url: str) -> bool:
        expected = str(expected_url or "").strip()
        current = str(current_url or "").strip()
        if not expected or not current:
            return False
        try:
            expected_host = (urlparse(expected).netloc or "").lower()
            current_host = (urlparse(current).netloc or "").lower()
        except Exception:
            return expected.rstrip("/") == current.rstrip("/")
        if expected_host.startswith("www."):
            expected_host = expected_host[4:]
        if current_host.startswith("www."):
            current_host = current_host[4:]
        if not expected_host or not current_host:
            return expected.rstrip("/") == current.rstrip("/")
        return (
            expected_host == current_host
            or expected_host.endswith(f".{current_host}")
            or current_host.endswith(f".{expected_host}")
        )

    @staticmethod
    def _task_requires_structured_data(task: Dict[str, Any]) -> bool:
        params = task.get("params", {}) if isinstance(task.get("params"), dict) else {}
        description = " ".join(
            str(item or "")
            for item in (
                task.get("description"),
                params.get("task"),
            )
        ).lower()
        retrieval_keywords = (
            "extract",
            "read",
            "show",
            "search",
            "find",
            "weather",
            "forecast",
            "temperature",
            "humidity",
            "air quality",
            "summary",
            "content",
            "提取",
            "读取",
            "展示",
            "搜索",
            "查询",
            "天气",
            "预报",
            "气温",
            "湿度",
            "空气质量",
            "总结",
            "内容",
        )
        interaction_keywords = (
            "login",
            "sign in",
            "register",
            "submit",
            "pay",
            "upload",
            "download",
            "登录",
            "注册",
            "提交",
            "支付",
            "上传",
            "下载",
        )
        if any(keyword in description for keyword in interaction_keywords):
            return False
        return any(keyword in description for keyword in retrieval_keywords)

    # ------------------------------------------------------------------
    # 单任务验证
    # ------------------------------------------------------------------
    def validate_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """
        验证单个已完成任务的结果。

        Returns:
            {"passed": bool, "failure_type": str|None, "issues": [str]}
        """
        issues: List[str] = []
        result = task.get("result")
        task_type = task.get("task_type", "")

        # --- 通用规则 ---
        if result is None:
            return {"passed": False, "failure_type": "unknown", "issues": ["result 为 None"]}

        if isinstance(result, dict) and not result.get("success"):
            issues.append(f"result.success 为 False: {result.get('error', '')[:120]}")
            return {"passed": False, "failure_type": task.get("failure_type", "unknown"), "issues": issues}

        # --- 按 worker 类型的专项规则 ---
        if task_type == "web_worker":
            data = result.get("data", []) if isinstance(result, dict) else []
            if not data:
                issues.append("web_worker: data 列表为空")
                return {"passed": False, "failure_type": "selector_not_found", "issues": issues}

        elif task_type == "file_worker":
            file_path = result.get("file_path", "") if isinstance(result, dict) else ""
            if file_path:
                p = Path(file_path)
                if not p.exists():
                    issues.append(f"file_worker: 文件不存在 {file_path}")
                    return {"passed": False, "failure_type": "permission_denied", "issues": issues}
                if p.stat().st_size == 0:
                    issues.append("file_worker: 文件为空")
                    return {"passed": False, "failure_type": "unknown", "issues": issues}

        elif task_type == "browser_agent":
            success = result.get("success", False) if isinstance(result, dict) else False
            if not success:
                issues.append("browser_agent: success 标志为 False")
                return {"passed": False, "failure_type": task.get("failure_type", "unknown"), "issues": issues}

            expected_url = str(result.get("expected_url", "") or "").strip() if isinstance(result, dict) else ""
            current_url = str(result.get("url", "") or "").strip() if isinstance(result, dict) else ""
            if expected_url and current_url and not self._urls_look_related(expected_url, current_url):
                issues.append(f"browser_agent: landed on unexpected url {current_url}")
                return {"passed": False, "failure_type": "navigation_error", "issues": issues}
            data = result.get("data", []) if isinstance(result, dict) else []
            if self._task_requires_structured_data(task) and not data:
                issues.append("browser_agent: expected extracted data but result.data is empty")
                return {"passed": False, "failure_type": "selector_not_found", "issues": issues}

        elif task_type == "system_worker":
            rc = result.get("return_code") if isinstance(result, dict) else None
            if rc is not None and rc != 0:
                issues.append(f"system_worker: return_code={rc}")
                return {"passed": False, "failure_type": "unknown", "issues": issues}

        return {"passed": True, "failure_type": None, "issues": issues}

    # ------------------------------------------------------------------
    # LangGraph 节点函数
    # ------------------------------------------------------------------
    def validate(self, state: OmniCoreState) -> OmniCoreState:
        """
        遍历所有 completed 任务，逐个硬规则验证。
        失败的任务 status → failed，设置 failure_type。
        设置 state["validator_passed"]。
        """
        log_agent_action(self.name, "开始硬规则验证")
        all_passed = True
        completed_count = 0
        failed_count = 0

        for idx, task in enumerate(state["task_queue"]):
            status = str(task.get("status", "") or "")
            if status == "failed":
                failed_count += 1
                all_passed = False
                continue

            if task["status"] != "completed":
                continue

            completed_count += 1
            vr = self.validate_task(task)
            if vr["passed"]:
                log_success(f"Validator PASS: {task['task_id']}")
            else:
                all_passed = False
                log_warning(f"Validator FAIL: {task['task_id']} — {vr['issues']}")
                state["task_queue"][idx]["status"] = "failed"
                state["task_queue"][idx]["failure_type"] = vr["failure_type"]
                state["task_queue"][idx]["failure_source"] = "validator"

        if state.get("task_queue") and completed_count == 0:
            # Keep waiting queues untouched; only fail fast when there are failed tasks
            # or when all tasks have reached terminal non-waiting states without success.
            statuses = {str(task.get("status", "") or "") for task in state.get("task_queue", [])}
            has_waiting = any(
                status in statuses
                for status in (WAITING_FOR_APPROVAL, WAITING_FOR_EVENT, BLOCKED)
            )
            has_failed = failed_count > 0 or "failed" in statuses
            all_terminal = all(
                status in {"completed", "failed", WAITING_FOR_APPROVAL, WAITING_FOR_EVENT, BLOCKED}
                for status in statuses
            )
            if has_failed or (all_terminal and not has_waiting):
                all_passed = False
                if failed_count == 0:
                    log_warning("Validator FAIL: no completed tasks in terminal task set")

        state["validator_passed"] = all_passed

        from langchain_core.messages import SystemMessage
        state["messages"].append(
            SystemMessage(content=f"Validator 验证结果: {'全部通过' if all_passed else '存在失败任务'}")
        )

        return state
