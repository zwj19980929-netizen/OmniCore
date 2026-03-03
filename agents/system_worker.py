"""
OmniCore System Worker Agent
负责系统级操作：执行脚本、模拟键鼠、操作应用程序
"""
import subprocess
import platform
from typing import Dict, Any, Optional
from pathlib import Path

from core.state import OmniCoreState, TaskItem
from utils.logger import log_agent_action, logger, log_success, log_error, log_warning
from utils.human_confirm import HumanConfirm
from config.settings import settings

def _import_paod():
    from agents.paod import classify_failure, make_trace_step, execute_fallback, MAX_FALLBACK_ATTEMPTS
    return classify_failure, make_trace_step, execute_fallback, MAX_FALLBACK_ATTEMPTS


class SystemWorker:
    """
    系统控制 Worker Agent
    处理系统级操作和脚本执行
    """

    def __init__(self):
        self.name = "SystemWorker"
        self.platform = platform.system()  # Windows, Darwin, Linux

    def _validate_command_safety(self, command: str) -> Optional[str]:
        normalized = str(command or "").strip()
        if not normalized:
            return "命令不能为空"
        for token in ["&&", "||", ";", "|", ">", "<", "`", "$("]:
            if token in normalized:
                return f"不允许的命令控制符: {token}"
        return None

    def execute_command(
        self,
        command: str,
        working_dir: Optional[str] = None,
        timeout: int = None,
        require_confirm: bool = True,
    ) -> Dict[str, Any]:
        """
        执行系统命令

        Args:
            command: 要执行的命令
            working_dir: 工作目录
            timeout: 超时时间（秒），None 则使用默认配置
            require_confirm: 是否需要人类确认

        Returns:
            执行结果
        """
        timeout_sec = timeout if timeout is not None else settings.SYSTEM_COMMAND_TIMEOUT
        validation_error = self._validate_command_safety(command)
        if validation_error:
            return {
                "success": False,
                "error": validation_error,
                "command": command,
            }
        log_agent_action(self.name, "准备执行命令", command[:50])

        # 高危操作确认
        if require_confirm and settings.REQUIRE_HUMAN_CONFIRM:
            confirmed = HumanConfirm.request_system_command_confirmation(
                command=command,
                working_dir=working_dir or "当前目录",
            )
            if not confirmed:
                return {
                    "success": False,
                    "error": "用户取消操作",
                    "command": command,
                }

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                cwd=working_dir,
            )

            if result.returncode == 0:
                log_success(f"命令执行成功: {command[:30]}")
                return {
                    "success": True,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "return_code": result.returncode,
                }
            else:
                log_error(f"命令执行失败: {result.stderr}")
                return {
                    "success": False,
                    "error": result.stderr,
                    "stdout": result.stdout,
                    "return_code": result.returncode,
                }

        except subprocess.TimeoutExpired:
            log_error(f"命令执行超时: {timeout_sec}秒")
            return {
                "success": False,
                "error": f"执行超时 ({timeout_sec}秒)",
                "command": command,
            }
        except Exception as e:
            log_error(f"命令执行异常: {e}")
            return {
                "success": False,
                "error": str(e),
                "command": command,
            }

    def simulate_keyboard(
        self,
        text: str,
        interval: float = 0.05,
    ) -> Dict[str, Any]:
        """
        模拟键盘输入

        Args:
            text: 要输入的文本
            interval: 按键间隔

        Returns:
            执行结果
        """
        log_agent_action(self.name, "模拟键盘输入", f"长度: {len(text)}")

        try:
            import pyautogui
            pyautogui.typewrite(text, interval=interval)
            log_success("键盘输入完成")
            return {
                "success": True,
                "text_length": len(text),
            }
        except Exception as e:
            log_error(f"键盘模拟失败: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    def simulate_mouse_click(
        self,
        x: int,
        y: int,
        button: str = "left",
        clicks: int = 1,
    ) -> Dict[str, Any]:
        """
        模拟鼠标点击

        Args:
            x: X 坐标
            y: Y 坐标
            button: 按钮 (left, right, middle)
            clicks: 点击次数

        Returns:
            执行结果
        """
        log_agent_action(self.name, "模拟鼠标点击", f"位置: ({x}, {y})")

        try:
            import pyautogui
            pyautogui.click(x=x, y=y, button=button, clicks=clicks)
            log_success(f"鼠标点击完成: ({x}, {y})")
            return {
                "success": True,
                "position": (x, y),
                "button": button,
            }
        except Exception as e:
            log_error(f"鼠标模拟失败: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    def take_screenshot(
        self,
        save_path: Optional[str] = None,
        region: Optional[tuple] = None,
    ) -> Dict[str, Any]:
        """
        截取屏幕截图

        Args:
            save_path: 保存路径
            region: 截取区域 (x, y, width, height)

        Returns:
            执行结果
        """
        log_agent_action(self.name, "截取屏幕", save_path or "内存")

        try:
            import pyautogui
            screenshot = pyautogui.screenshot(region=region)

            if save_path:
                path = Path(save_path).expanduser()
                path.parent.mkdir(parents=True, exist_ok=True)
                screenshot.save(str(path))
                log_success(f"截图已保存: {path}")
                return {
                    "success": True,
                    "file_path": str(path),
                    "size": screenshot.size,
                }
            else:
                return {
                    "success": True,
                    "image": screenshot,
                    "size": screenshot.size,
                }

        except Exception as e:
            log_error(f"截图失败: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    def execute(self, task: TaskItem, shared_memory: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行系统任务（PAOD 增强：fallback + trace）

        Args:
            task: 任务项
            shared_memory: 共享内存

        Returns:
            执行结果
        """
        classify_failure, make_trace_step, execute_fallback, MAX_FALLBACK_ATTEMPTS = _import_paod()

        params = task["params"]
        action = params.get("action", "")
        trace = task.get("execution_trace", [])
        step_no = len(trace) + 1

        log_agent_action(self.name, f"执行任务: {action}", task["description"])

        # --- 主执行 ---
        trace.append(make_trace_step(step_no, f"execute {action}", str(params)[:100], "", ""))
        dispatch_params = dict(params)
        if action == "execute_command" and task.get("requires_confirmation", False):
            confirmed = HumanConfirm.request_system_command_confirmation(
                command=dispatch_params.get("command", ""),
                working_dir=dispatch_params.get("working_dir", "当前目录"),
            )
            if not confirmed:
                result = {
                    "success": False,
                    "error": "用户取消系统命令执行",
                    "command": dispatch_params.get("command", ""),
                }
                trace[-1]["observation"] = "success=False, cancelled_by_user"
                trace[-1]["decision"] = "cancelled"
                task["failure_type"] = classify_failure(result.get("error", ""))
                task["execution_trace"] = trace
                return result
            dispatch_params["_policy_preconfirmed"] = True

        result = self._dispatch_action(action, dispatch_params)
        trace[-1]["observation"] = f"success={result.get('success')}, rc={result.get('return_code', 'N/A')}"

        if result.get("success"):
            trace[-1]["decision"] = "done"
            task["execution_trace"] = trace
            return result

        # --- 失败 → 尝试 fallback ---
        trace[-1]["decision"] = "failed → try fallback"
        fb_index = 0
        while fb_index < MAX_FALLBACK_ATTEMPTS:
            fb = execute_fallback(task, fb_index, shared_memory)
            if fb is None or fb["action"] != "retry":
                break
            fb_index += 1
            step_no += 1

            patch = fb.get("param_patch", {})
            patched_params = {**params, **patch}
            patched_action = patched_params.get("action", action)
            trace.append(make_trace_step(step_no, f"retry #{fb_index}", f"patch={patch}", "", ""))

            result = self._dispatch_action(patched_action, patched_params)
            trace[-1]["observation"] = f"success={result.get('success')}"

            if result.get("success"):
                trace[-1]["decision"] = "done"
                task["execution_trace"] = trace
                return result
            trace[-1]["decision"] = "still_failing"

        # --- 所有 fallback 耗尽 ---
        task["failure_type"] = classify_failure(result.get("error", ""))
        task["execution_trace"] = trace
        return result

    def _dispatch_action(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """根据 action 分发到具体执行方法"""
        if action == "execute_command":
            return self.execute_command(
                command=params.get("command", ""),
                working_dir=params.get("working_dir"),
                timeout=params.get("timeout", 30),
                require_confirm=not params.get("_policy_preconfirmed", False),
            )
        elif action == "keyboard":
            return self.simulate_keyboard(
                text=params.get("text", ""),
                interval=params.get("interval", 0.05),
            )
        elif action == "mouse_click":
            return self.simulate_mouse_click(
                x=params.get("x", 0),
                y=params.get("y", 0),
                button=params.get("button", "left"),
                clicks=params.get("clicks", 1),
            )
        elif action == "screenshot":
            return self.take_screenshot(
                save_path=params.get("save_path"),
                region=params.get("region"),
            )
        else:
            return {"success": False, "error": f"未知操作类型: {action}"}

    def process(self, state: OmniCoreState) -> OmniCoreState:
        """
        LangGraph 节点函数：处理系统相关任务
        """
        classify_failure = _import_paod()[0]

        for idx, task in enumerate(state["task_queue"]):
            if task["task_type"] == "system_worker" and task["status"] == "pending":
                state["task_queue"][idx]["status"] = "running"

                result = self.execute(task, state["shared_memory"])

                state["task_queue"][idx]["status"] = (
                    "completed" if result.get("success") else "failed"
                )
                state["task_queue"][idx]["result"] = result

                state["shared_memory"][task["task_id"]] = result

                if not result.get("success"):
                    state["task_queue"][idx]["failure_type"] = task.get("failure_type") or classify_failure(result.get("error", ""))
                    state["error_trace"] = result.get("error", "未知错误")

        return state
