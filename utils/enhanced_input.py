"""
增强的命令行输入模块 - 提供更好的交互体验
支持：历史记录、自动补全、多行输入、快捷键
"""
import os
import sys
from typing import List, Optional


def _is_libedit(readline_module) -> bool:
    """检测 readline 后端是否为 macOS libedit（而非 GNU readline）"""
    doc = getattr(readline_module, "__doc__", "") or ""
    return "libedit" in doc


def _env_flag_enabled(name: str) -> bool:
    value = (os.getenv(name) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


class EnhancedInput:
    """增强的命令行输入"""

    def __init__(self, history_file: str = "~/.omnicore_history"):
        self.history_file = os.path.expanduser(history_file)
        self.has_readline = False
        self.readline = None
        self.readline_disabled_reason: Optional[str] = None
        self._setup_readline()

    def _setup_readline(self):
        """设置 readline 支持"""
        try:
            if sys.platform == "win32" and not _env_flag_enabled("OMNICORE_ENABLE_PYREADLINE3"):
                self.has_readline = False
                self.readline_disabled_reason = "windows_pyreadline3_disabled"
                return

            import readline

            # pyreadline3 on Windows can raise TypeError/AttributeError during
            # initialization when the console size cannot be determined.
            # Trigger a lightweight probe to surface any such crash early.
            if sys.platform == "win32":
                try:
                    readline.get_current_history_length()
                except Exception:
                    self.has_readline = False
                    self.readline_disabled_reason = "readline_probe_failed"
                    return

            self.readline = readline
            self.has_readline = True
            self.readline_disabled_reason = None

            # 加载历史记录
            if os.path.exists(self.history_file):
                try:
                    readline.read_history_file(self.history_file)
                except Exception:
                    pass

            # 设置历史记录最大条数
            readline.set_history_length(1000)

            # 根据实际后端选择正确的绑定语法
            if _is_libedit(readline):
                readline.parse_and_bind("bind ^I rl_complete")
            else:
                readline.parse_and_bind("tab: complete")

            # 设置补全函数
            readline.set_completer(self._completer)

        except (ImportError, Exception):
            self.has_readline = False
            self.readline_disabled_reason = "readline_unavailable"

    def _completer(self, text: str, state: int) -> Optional[str]:
        """自动补全函数"""
        # 常用命令列表
        commands = [
            "quit", "exit", "help", "status", "history", "clear",
            "搜索", "抓取", "保存", "读取", "执行"
        ]

        matches = [cmd for cmd in commands if cmd.startswith(text)]

        if state < len(matches):
            return matches[state]
        return None

    def input(self, prompt: str = "> ") -> str:
        """增强的输入函数"""
        try:
            return input(prompt).strip()
        except (TypeError, AttributeError):
            # pyreadline3 on Windows can crash inside input() when the console
            # size cannot be determined (size() returns None). Disable readline
            # and fall back to plain input for the rest of the session.
            self.has_readline = False
            self.readline_disabled_reason = "readline_runtime_failure"
            try:
                import readline as _rl
                _rl.set_completer(None)
            except Exception:
                pass
            return self._plain_input(prompt)

    @staticmethod
    def _plain_input(prompt: str) -> str:
        sys.stdout.write(prompt)
        sys.stdout.flush()
        line = sys.stdin.readline()
        if line == "":
            raise EOFError
        return line.strip()

    def save_history(self):
        """保存历史记录"""
        if self.has_readline:
            try:
                self.readline.write_history_file(self.history_file)
            except Exception:
                pass

    def clear_history(self):
        """清除历史记录"""
        if self.has_readline:
            self.readline.clear_history()
            if os.path.exists(self.history_file):
                try:
                    os.remove(self.history_file)
                except Exception:
                    pass

    def get_history(self, limit: int = 10) -> List[str]:
        """获取历史记录"""
        if not self.has_readline:
            return []

        history = []
        length = self.readline.get_current_history_length()
        start = max(1, length - limit + 1)

        for i in range(start, length + 1):
            try:
                item = self.readline.get_history_item(i)
                if item:
                    history.append(item)
            except Exception:
                pass

        return history


def install_readline_if_needed():
    """检查并提示安装 readline"""
    try:
        import readline
        return True
    except ImportError:
        print("\n提示：为了获得更好的命令行体验（历史记录、上下键浏览等），")
        print("建议安装 readline 支持：")
        print()
        print("  macOS:")
        print("    brew install readline")
        print("    pip install gnureadline")
        print()
        print("  Linux:")
        print("    sudo apt-get install libreadline-dev  # Debian/Ubuntu")
        print("    sudo yum install readline-devel       # CentOS/RHEL")
        print()
        return False


# 使用示例
if __name__ == "__main__":
    enhanced_input = EnhancedInput()

    print("增强命令行测试")
    print("提示：使用上下键浏览历史，Tab 键自动补全，Ctrl+C 或 Ctrl+D 退出")
    print()

    while True:
        try:
            user_input = enhanced_input.input("测试 > ")

            if user_input.lower() in ["quit", "exit"]:
                print("再见！")
                break

            if user_input == "history":
                print("\n最近的命令：")
                for i, cmd in enumerate(enhanced_input.get_history(), 1):
                    print(f"  {i}. {cmd}")
                continue

            if user_input == "clear":
                enhanced_input.clear_history()
                print("历史记录已清除")
                continue

            print(f"你输入了: {user_input}")

        except KeyboardInterrupt:
            print("\n再见！")
            break

    enhanced_input.save_history()
