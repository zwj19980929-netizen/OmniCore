"""
OmniCore 沙盒执行环境
用于安全执行用户提供的代码片段
"""
from typing import Dict, Any, Optional
from io import StringIO
import sys

from RestrictedPython import compile_restricted, safe_globals
from RestrictedPython.Eval import default_guarded_getiter
from RestrictedPython.Guards import guarded_iter_unpack_sequence, safer_getattr

from utils.logger import log_agent_action, logger, log_warning


class Sandbox:
    """
    受限 Python 沙盒
    使用 RestrictedPython 安全执行代码
    """

    def __init__(self):
        self.name = "Sandbox"
        self._setup_safe_builtins()

    def _setup_safe_builtins(self):
        """配置安全的内置函数"""
        self.safe_builtins = safe_globals.copy()

        # 添加安全的内置函数
        self.safe_builtins["_getiter_"] = default_guarded_getiter
        self.safe_builtins["_iter_unpack_sequence_"] = guarded_iter_unpack_sequence
        self.safe_builtins["_getattr_"] = safer_getattr

        # 允许的安全模块
        self.allowed_modules = {
            "math",
            "json",
            "datetime",
            "re",
            "collections",
        }

    def _safe_import(self, name: str, *args, **kwargs):
        """安全的 import 函数"""
        if name in self.allowed_modules:
            return __import__(name, *args, **kwargs)
        raise ImportError(f"模块 '{name}' 不允许在沙盒中使用")

    def execute(
        self,
        code: str,
        local_vars: Optional[Dict[str, Any]] = None,
        timeout: int = 5,
    ) -> Dict[str, Any]:
        """
        在沙盒中执行代码

        Args:
            code: 要执行的 Python 代码
            local_vars: 传入的局部变量
            timeout: 超时时间（秒）

        Returns:
            执行结果
        """
        log_agent_action(self.name, "执行代码", f"长度: {len(code)}")

        # 捕获输出
        old_stdout = sys.stdout
        sys.stdout = captured_output = StringIO()

        try:
            # 编译受限代码
            byte_code = compile_restricted(
                code,
                filename="<sandbox>",
                mode="exec",
            )

            if byte_code.errors:
                return {
                    "success": False,
                    "error": f"编译错误: {byte_code.errors}",
                    "output": "",
                }

            # 准备执行环境
            exec_globals = self.safe_builtins.copy()
            exec_globals["__builtins__"]["__import__"] = self._safe_import

            exec_locals = local_vars.copy() if local_vars else {}

            # 执行代码
            exec(byte_code.code, exec_globals, exec_locals)

            output = captured_output.getvalue()

            return {
                "success": True,
                "output": output,
                "locals": {
                    k: v for k, v in exec_locals.items()
                    if not k.startswith("_")
                },
            }

        except Exception as e:
            logger.error(f"沙盒执行失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "output": captured_output.getvalue(),
            }

        finally:
            sys.stdout = old_stdout

    def evaluate(
        self,
        expression: str,
        local_vars: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        在沙盒中计算表达式

        Args:
            expression: 要计算的表达式
            local_vars: 传入的局部变量

        Returns:
            计算结果
        """
        log_agent_action(self.name, "计算表达式", expression[:50])

        try:
            byte_code = compile_restricted(
                expression,
                filename="<sandbox>",
                mode="eval",
            )

            if byte_code.errors:
                return {
                    "success": False,
                    "error": f"编译错误: {byte_code.errors}",
                }

            exec_globals = self.safe_builtins.copy()
            exec_locals = local_vars.copy() if local_vars else {}

            result = eval(byte_code.code, exec_globals, exec_locals)

            return {
                "success": True,
                "result": result,
            }

        except Exception as e:
            logger.error(f"表达式计算失败: {e}")
            return {
                "success": False,
                "error": str(e),
            }
