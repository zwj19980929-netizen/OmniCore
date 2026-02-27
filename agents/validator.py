"""
OmniCore Validator — 纯 Python 硬规则验证层
零 LLM 调用，确定性检查。在 Critic（LLM 审查）之前运行。
"""
from typing import Dict, Any, List
from pathlib import Path

from core.state import OmniCoreState
from utils.logger import log_agent_action, log_success, log_warning


class Validator:
    """确定性硬规则验证器"""

    def __init__(self):
        self.name = "Validator"

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

        for idx, task in enumerate(state["task_queue"]):
            if task["status"] != "completed":
                continue

            vr = self.validate_task(task)
            if vr["passed"]:
                log_success(f"Validator PASS: {task['task_id']}")
            else:
                all_passed = False
                log_warning(f"Validator FAIL: {task['task_id']} — {vr['issues']}")
                state["task_queue"][idx]["status"] = "failed"
                state["task_queue"][idx]["failure_type"] = vr["failure_type"]

        state["validator_passed"] = all_passed

        from langchain_core.messages import SystemMessage
        state["messages"].append(
            SystemMessage(content=f"Validator 验证结果: {'全部通过' if all_passed else '存在失败任务'}")
        )

        return state
