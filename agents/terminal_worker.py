"""
OmniCore Terminal Worker Agent
Claude Code 级别的终端操作能力：完整 shell 执行、流式输出、文件操作、搜索
与 system_worker 并存：system_worker 负责 GUI 自动化（键鼠/截图），
terminal_worker 专注 shell 命令执行与文件系统操作。
"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from config.settings import settings
from core.constants import TerminalAction, TerminalPermissionLevel
from core.state import OmniCoreState, TaskItem
from utils.logger import log_agent_action, log_error, log_success, log_warning, logger

# ──────────────────────────────────────────────
# OS 感知：运行时检测一次，全局复用
# ──────────────────────────────────────────────

_OS_SYSTEM = platform.system()   # "Darwin" / "Linux" / "Windows"
_IS_MACOS   = _OS_SYSTEM == "Darwin"
_IS_LINUX   = _OS_SYSTEM == "Linux"
_IS_WINDOWS = _OS_SYSTEM == "Windows"

def _detect_shell() -> str:
    """返回当前最合适的 shell 路径。"""
    # 1. 用户配置优先
    if settings.TERMINAL_SHELL and settings.TERMINAL_SHELL != os.environ.get("SHELL", ""):
        return settings.TERMINAL_SHELL
    # 2. 环境变量 $SHELL
    env_shell = os.environ.get("SHELL", "")
    if env_shell and shutil.which(env_shell):
        return env_shell
    # 3. OS 默认
    if _IS_WINDOWS:
        return shutil.which("powershell") or "cmd.exe"
    if _IS_MACOS:
        return shutil.which("zsh") or shutil.which("bash") or "/bin/sh"
    return shutil.which("bash") or shutil.which("sh") or "/bin/sh"

_DETECTED_SHELL = _detect_shell()

# ──────────────────────────────────────────────
# 权限规则表（balanced 模式）
# ──────────────────────────────────────────────

_AUTO_ALLOW_PREFIXES = (
    "ls", "ll", "la", "cat", "head", "tail", "less", "more",
    "echo", "printf", "pwd", "which", "type", "file", "wc",
    "find", "grep", "rg", "ripgrep", "awk", "sed",
    "git status", "git log", "git diff", "git branch", "git show",
    "git remote", "git fetch", "git stash list",
    "python --version", "python3 --version", "node --version",
    "npm list", "pip list", "pip show", "pip freeze",
    "env", "printenv", "uname", "hostname", "date", "uptime",
    "ps", "top", "htop", "df", "du", "free",
    "curl", "wget",  # 网络读取，通常安全
)

_NOTIFY_PREFIXES = (
    "mkdir", "touch", "cp", "mv",
    "git add", "git commit", "git stash",
    "npm install", "npm ci", "npm update",
    "pip install", "pip3 install", "pip upgrade", "pip3 upgrade",
    "python ", "python3 ", "node ", "npm run", "npm test", "npm build",
    "brew install", "brew upgrade", "brew update",
    "apt install", "apt-get install", "yum install", "dnf install",
    "cargo add", "cargo install",
    "go get", "go install",
    "make", "cargo build", "cargo test", "go build", "go test",
    "pytest", "jest", "mocha",
    "cd",
)

_CONFIRM_PREFIXES = (
    "rm ", "rm\t", "rmdir",
    "git push", "git reset", "git rebase", "git clean",
    "git checkout -- ", "git restore",
    "chmod", "chown", "sudo",
    "kill", "pkill", "killall",
    "docker rm", "docker rmi", "docker stop",
    "mv /", "cp /",
    "> /", ">> /",
)


def _classify_command_permission(
    command: str,
    mode: str = "balanced",
    sandbox_enabled: bool = False,
    sandbox_root: str = "",
    session_approvals: Optional[set] = None,
    user_auto_allow: tuple = (),
    user_always_confirm: tuple = (),
) -> TerminalPermissionLevel:
    """
    根据命令内容和配置，判定权限级别。

    Args:
        command: 要执行的命令
        mode: 权限模式 strict/balanced/permissive
        sandbox_enabled: 是否启用沙箱
        sandbox_root: 沙箱根目录
        session_approvals: 会话内已审批的命令类别
        user_auto_allow: 用户自定义自动放行前缀
        user_always_confirm: 用户自定义强制确认前缀

    Returns:
        TerminalPermissionLevel
    """
    if mode == "strict":
        return TerminalPermissionLevel.REQUIRE_CONFIRM
    if mode == "permissive":
        # 仅对明确危险的操作确认
        normalized = command.strip().lower()
        if any(normalized.startswith(p) for p in _CONFIRM_PREFIXES):
            return TerminalPermissionLevel.REQUIRE_CONFIRM
        return TerminalPermissionLevel.AUTO_ALLOW

    # balanced 模式（默认）
    normalized = command.strip().lower()

    # 管道远程执行检测：curl/wget | bash/sh/python 等，无论前缀如何都强制确认
    _PIPE_EXEC_PATTERN = re.compile(
        r"(curl|wget)\b.+\|\s*(bash|sh|zsh|fish|python\d?|ruby|perl|node)\b",
        re.IGNORECASE,
    )
    if _PIPE_EXEC_PATTERN.search(command):
        return TerminalPermissionLevel.REQUIRE_CONFIRM

    # 用户自定义优先
    if user_always_confirm and any(normalized.startswith(p.lower()) for p in user_always_confirm):
        return TerminalPermissionLevel.REQUIRE_CONFIRM
    if user_auto_allow and any(normalized.startswith(p.lower()) for p in user_auto_allow):
        return TerminalPermissionLevel.AUTO_ALLOW

    # 会话内已审批
    if session_approvals:
        for approved_prefix in session_approvals:
            if normalized.startswith(approved_prefix.lower()):
                return TerminalPermissionLevel.AUTO_ALLOW

    # 强制确认
    if any(normalized.startswith(p) for p in _CONFIRM_PREFIXES):
        return TerminalPermissionLevel.REQUIRE_CONFIRM

    # 通知放行
    if any(normalized.startswith(p) for p in _NOTIFY_PREFIXES):
        return TerminalPermissionLevel.NOTIFY

    # 只读自动放行
    if any(normalized.startswith(p) for p in _AUTO_ALLOW_PREFIXES):
        return TerminalPermissionLevel.AUTO_ALLOW

    # 未匹配 → 通知放行（保守）
    return TerminalPermissionLevel.NOTIFY


# ──────────────────────────────────────────────
# TerminalWorker 主类
# ──────────────────────────────────────────────

class TerminalWorker:
    """
    Claude Code 级别的终端执行能力。

    支持：
    - 完整 shell 语法（管道 | 链式 && 重定向 > 通配符 *）
    - 流式实时输出
    - 长时间运行（最大 10 分钟）
    - 会话内持久化工作目录
    - 文件读写/编辑/搜索（Read/Write/Edit/Glob/Grep）
    - 三级权限控制
    """

    def __init__(self):
        self.name = "TerminalWorker"
        self._working_dir: Path = Path(
            settings.TERMINAL_SANDBOX_ROOT if settings.TERMINAL_SANDBOX_ENABLED else os.getcwd()
        )
        self._env: Dict[str, str] = {**os.environ}
        self._session_approvals: set = set()
        self._shell: str = _DETECTED_SHELL
        self._os_system: str = _OS_SYSTEM

        log_agent_action(
            self.name,
            "初始化",
            f"OS={_OS_SYSTEM}, shell={self._shell}, cwd={self._working_dir}",
        )

    # ──────────────────────── Shell 执行 ────────────────────────

    def execute_shell(
        self,
        command: str,
        working_dir: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[Dict[str, str]] = None,
        stream_callback: Optional[Callable[[str, str], None]] = None,
        require_confirm: bool = True,
        permission_level: Optional[TerminalPermissionLevel] = None,
    ) -> Dict[str, Any]:
        """
        执行 shell 命令（完整语法支持）。

        Args:
            command: shell 命令（支持管道、链式、重定向等完整语法）
            working_dir: 工作目录，None 则使用会话工作目录
            timeout: 超时秒数，None 则使用配置默认值
            env: 额外环境变量
            stream_callback: 实时输出回调 (line, stream_type) -> None
            require_confirm: 是否需要确认（由权限分类决定时传 False 跳过）
            permission_level: 已预先分类的权限级别

        Returns:
            {success, stdout, stderr, return_code, command, working_dir}
        """
        if not command or not command.strip():
            return {"success": False, "error": "命令不能为空", "command": command}

        timeout_sec = min(
            timeout if timeout is not None else settings.TERMINAL_DEFAULT_TIMEOUT,
            settings.TERMINAL_MAX_TIMEOUT,
        )
        cwd = Path(working_dir).expanduser() if working_dir else self._working_dir
        exec_env = {**self._env, **(env or {})}

        # 权限判定
        if permission_level is None:
            permission_level = _classify_command_permission(
                command,
                mode=settings.TERMINAL_PERMISSION_MODE,
                sandbox_enabled=settings.TERMINAL_SANDBOX_ENABLED,
                sandbox_root=settings.TERMINAL_SANDBOX_ROOT,
                session_approvals=self._session_approvals,
                user_auto_allow=settings.TERMINAL_AUTO_ALLOW_PATTERNS,
                user_always_confirm=settings.TERMINAL_ALWAYS_CONFIRM_PATTERNS,
            )

        # 沙箱检查（写操作限制在沙箱目录内）
        if settings.TERMINAL_SANDBOX_ENABLED and permission_level != TerminalPermissionLevel.AUTO_ALLOW:
            sandbox = Path(settings.TERMINAL_SANDBOX_ROOT).expanduser().resolve()
            try:
                cwd.resolve().relative_to(sandbox)
            except ValueError:
                return {
                    "success": False,
                    "error": f"沙箱模式：工作目录 {cwd} 不在沙箱范围 {sandbox} 内",
                    "command": command,
                }

        # 权限门控
        if require_confirm and permission_level == TerminalPermissionLevel.REQUIRE_CONFIRM:
            from utils.human_confirm import HumanConfirm
            confirmed = HumanConfirm.request_terminal_command_confirmation(
                command=command,
                working_dir=str(cwd),
                risk_level="high",
            )
            if not confirmed:
                return {"success": False, "error": "用户取消执行", "command": command}
        elif permission_level == TerminalPermissionLevel.NOTIFY:
            log_agent_action(self.name, f"执行命令 [{permission_level}]", command[:80])

        log_agent_action(self.name, "shell", f"$ {command[:100]}")

        try:
            stdout_lines: List[str] = []
            stderr_lines: List[str] = []

            proc = subprocess.Popen(
                command,
                shell=True,
                executable=self._shell if not _IS_WINDOWS else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(cwd),
                env=exec_env,
            )

            # 流式读取（使用线程同时读 stdout/stderr 避免死锁）
            import threading

            def _drain(pipe, lines, tag):
                for line in pipe:
                    lines.append(line)
                    if stream_callback and settings.TERMINAL_STREAM_OUTPUT:
                        try:
                            stream_callback(line, tag)
                        except Exception:
                            pass

            t_out = threading.Thread(target=_drain, args=(proc.stdout, stdout_lines, "stdout"), daemon=True)
            t_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_lines, "stderr"), daemon=True)
            t_out.start()
            t_err.start()

            try:
                proc.wait(timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                return {
                    "success": False,
                    "error": f"执行超时（{timeout_sec}s）",
                    "stdout": "".join(stdout_lines),
                    "stderr": "".join(stderr_lines),
                    "return_code": -1,
                    "command": command,
                    "working_dir": str(cwd),
                }

            t_out.join(timeout=5)
            t_err.join(timeout=5)

            stdout = "".join(stdout_lines)
            stderr = "".join(stderr_lines)
            rc = proc.returncode

            if rc == 0:
                log_success(f"命令执行成功: {command[:40]}")
                return {
                    "success": True,
                    "stdout": stdout,
                    "stderr": stderr,
                    "return_code": rc,
                    "command": command,
                    "working_dir": str(cwd),
                }
            else:
                log_error(f"命令返回非零: rc={rc}, stderr={stderr[:100]}")
                return {
                    "success": False,
                    "error": stderr.strip() or f"命令返回 {rc}",
                    "stdout": stdout,
                    "stderr": stderr,
                    "return_code": rc,
                    "command": command,
                    "working_dir": str(cwd),
                }

        except Exception as exc:
            log_error(f"命令执行异常: {exc}")
            return {"success": False, "error": str(exc), "command": command}

    def change_directory(self, path: str) -> Dict[str, Any]:
        """切换会话工作目录（持久化）"""
        target = Path(path).expanduser()
        if not target.is_absolute():
            target = self._working_dir / target
        target = target.resolve()
        if not target.is_dir():
            return {"success": False, "error": f"目录不存在: {target}"}
        self._working_dir = target
        log_success(f"工作目录切换到: {target}")
        return {"success": True, "working_dir": str(target)}

    # ──────────────────────── 文件操作 ────────────────────────

    def read_file(
        self,
        file_path: str,
        offset: int = 0,
        limit: Optional[int] = None,
        encoding: str = "utf-8",
    ) -> Dict[str, Any]:
        """
        读取文件内容（带行号，支持分页）。

        Args:
            file_path: 文件路径
            offset: 起始行号（从 1 开始）
            limit: 最大读取行数，None 表示全部
            encoding: 文件编码
        """
        path = self._resolve_path(file_path)
        if not path.exists():
            return {"success": False, "error": f"文件不存在: {path}"}
        if not path.is_file():
            return {"success": False, "error": f"不是文件: {path}"}

        try:
            with path.open(encoding=encoding, errors="replace") as f:
                all_lines = f.readlines()

            start = max(0, offset - 1) if offset > 0 else 0
            end = (start + limit) if limit else len(all_lines)
            selected = all_lines[start:end]

            # 带行号格式（类 cat -n）
            numbered = "".join(
                f"{start + i + 1:6}\t{line}" for i, line in enumerate(selected)
            )
            return {
                "success": True,
                "content": numbered,
                "raw_content": "".join(selected),
                "file_path": str(path),
                "total_lines": len(all_lines),
                "lines_returned": len(selected),
                "offset": start + 1,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc), "file_path": str(path)}

    def write_file(
        self,
        file_path: str,
        content: str,
        encoding: str = "utf-8",
        require_confirm: bool = True,
    ) -> Dict[str, Any]:
        """写入文件（自动创建目录）"""
        path = self._resolve_path(file_path)
        is_overwrite = path.exists()

        if require_confirm:
            from utils.human_confirm import HumanConfirm
            confirmed = HumanConfirm.request_file_write_confirmation(
                file_path=str(path),
                content_preview=content[:200],
                is_overwrite=is_overwrite,
            )
            if not confirmed:
                return {"success": False, "error": "用户取消写入", "file_path": str(path)}

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding=encoding)
            log_success(f"文件写入成功: {path}")
            return {
                "success": True,
                "file_path": str(path),
                "size": len(content),
                "is_overwrite": is_overwrite,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc), "file_path": str(path)}

    def edit_file(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        require_confirm: bool = False,
    ) -> Dict[str, Any]:
        """
        精确字符串替换（类似 Claude Code 的 Edit 工具）。

        Args:
            file_path: 文件路径
            old_string: 要替换的精确字符串
            new_string: 替换为的字符串
            replace_all: 是否替换所有出现（默认只替换第一个）
        """
        path = self._resolve_path(file_path)
        if not path.exists():
            return {"success": False, "error": f"文件不存在: {path}"}

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return {"success": False, "error": f"读取文件失败: {exc}"}

        count = content.count(old_string)
        if count == 0:
            return {"success": False, "error": f"未找到要替换的字符串: {old_string[:50]!r}", "file_path": str(path)}
        if not replace_all and count > 1:
            return {
                "success": False,
                "error": f"找到 {count} 处匹配，请提供更多上下文使其唯一，或设置 replace_all=true",
                "file_path": str(path),
                "match_count": count,
            }

        if replace_all:
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        if require_confirm:
            from utils.human_confirm import HumanConfirm
            confirmed = HumanConfirm.request_file_write_confirmation(
                file_path=str(path),
                content_preview=f"替换 {count} 处: {old_string[:80]!r} → {new_string[:80]!r}",
                is_overwrite=True,
            )
            if not confirmed:
                return {"success": False, "error": "用户取消编辑"}

        try:
            path.write_text(new_content, encoding="utf-8")
            log_success(f"文件编辑成功: {path}，替换 {count} 处")
            return {
                "success": True,
                "file_path": str(path),
                "replacements": count if replace_all else 1,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    # ──────────────────────── 搜索 ────────────────────────

    def glob_search(
        self,
        pattern: str,
        path: Optional[str] = None,
        max_results: int = 100,
    ) -> Dict[str, Any]:
        """
        文件模式搜索（支持 **/*.py 等 glob 模式）。

        Args:
            pattern: glob 模式，如 "**/*.py" 或 "src/**/*.ts"
            path: 搜索根目录，None 则使用当前工作目录
            max_results: 最大返回数量
        """
        base = Path(path).expanduser() if path else self._working_dir
        if not base.is_dir():
            return {"success": False, "error": f"目录不存在: {base}"}

        try:
            matches = sorted(
                (str(p) for p in base.rglob(pattern) if not any(
                    part.startswith(".") for part in p.relative_to(base).parts
                )),
                key=lambda p: (Path(p).stat().st_mtime if Path(p).exists() else 0),
                reverse=True,
            )[:max_results]

            return {
                "success": True,
                "matches": matches,
                "count": len(matches),
                "pattern": pattern,
                "base_dir": str(base),
            }
        except Exception as exc:
            return {"success": False, "error": str(exc), "pattern": pattern}

    def grep_search(
        self,
        pattern: str,
        path: Optional[str] = None,
        include: Optional[str] = None,
        case_insensitive: bool = False,
        max_results: int = 50,
        context_lines: int = 0,
    ) -> Dict[str, Any]:
        """
        内容正则搜索（类似 ripgrep）。

        Args:
            pattern: 正则表达式
            path: 搜索路径（文件或目录）
            include: 文件过滤 glob，如 "*.py"
            case_insensitive: 是否忽略大小写
            max_results: 最大匹配数
            context_lines: 上下文行数
        """
        base = Path(path).expanduser() if path else self._working_dir
        if not base.exists():
            return {"success": False, "error": f"路径不存在: {base}"}

        try:
            flags = re.IGNORECASE if case_insensitive else 0
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            return {"success": False, "error": f"正则表达式错误: {exc}", "pattern": pattern}

        def _search_file(file_path: Path) -> List[Dict]:
            results = []
            try:
                lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
                for i, line in enumerate(lines):
                    if compiled.search(line):
                        ctx_start = max(0, i - context_lines)
                        ctx_end = min(len(lines), i + context_lines + 1)
                        results.append({
                            "file": str(file_path),
                            "line_number": i + 1,
                            "line": line,
                            "context_before": lines[ctx_start:i] if context_lines else [],
                            "context_after": lines[i + 1:ctx_end] if context_lines else [],
                        })
            except Exception:
                pass
            return results

        all_results: List[Dict] = []
        if base.is_file():
            all_results = _search_file(base)
        else:
            for file_path in sorted(base.rglob("*")):
                if not file_path.is_file():
                    continue
                if include and not fnmatch.fnmatch(file_path.name, include):
                    continue
                all_results.extend(_search_file(file_path))
                if len(all_results) >= max_results:
                    break

        return {
            "success": True,
            "matches": all_results[:max_results],
            "count": len(all_results),
            "pattern": pattern,
            "base_dir": str(base),
        }

    def list_dir(
        self,
        path: Optional[str] = None,
        show_hidden: bool = False,
    ) -> Dict[str, Any]:
        """列出目录内容"""
        base = Path(path).expanduser() if path else self._working_dir
        if not base.is_dir():
            return {"success": False, "error": f"目录不存在: {base}"}

        try:
            entries = []
            for entry in sorted(base.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
                if not show_hidden and entry.name.startswith("."):
                    continue
                stat = entry.stat()
                entries.append({
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                    "size": stat.st_size if entry.is_file() else None,
                })
            return {
                "success": True,
                "entries": entries,
                "count": len(entries),
                "path": str(base),
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    # ──────────────────────── 内部辅助 ────────────────────────

    def _resolve_path(self, file_path: str) -> Path:
        """解析路径：支持 ~，相对路径基于当前工作目录"""
        p = Path(file_path).expanduser()
        if not p.is_absolute():
            p = self._working_dir / p
        return p

    def approve_command_prefix(self, prefix: str) -> None:
        """会话内批准某个命令前缀，后续同类操作自动放行"""
        self._session_approvals.add(prefix.strip().lower())

    @property
    def working_dir(self) -> str:
        return str(self._working_dir)

    @property
    def shell(self) -> str:
        return self._shell

    @property
    def os_system(self) -> str:
        return self._os_system

    # ──────────────────────── Task 执行入口 ────────────────────────

    def execute(self, task: TaskItem, shared_memory: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行终端任务（PAOD 兼容：fallback + trace）。
        """
        from agents.paod import classify_failure, make_trace_step, execute_fallback, MAX_FALLBACK_ATTEMPTS

        params = task.get("params", {}) or {}
        action = str(params.get("action", TerminalAction.SHELL))
        trace = task.get("execution_trace", [])
        step_no = len(trace) + 1

        log_agent_action(self.name, f"执行任务 [{action}]", task.get("description", "")[:60])
        trace.append(make_trace_step(step_no, f"execute {action}", str(params)[:100], "", ""))

        result = self._dispatch_action(action, params)
        trace[-1]["observation"] = f"success={result.get('success')}, rc={result.get('return_code', 'N/A')}"

        if result.get("success"):
            trace[-1]["decision"] = "done"
            task["execution_trace"] = trace
            return result

        # fallback 循环
        trace[-1]["decision"] = "failed → try fallback"
        fb_index = 0
        while fb_index < MAX_FALLBACK_ATTEMPTS:
            fb = execute_fallback(task, fb_index, shared_memory)
            if fb is None or fb.get("action") != "retry":
                break
            fb_index += 1
            step_no += 1
            patch = fb.get("param_patch", {})
            patched_params = {**params, **patch}
            trace.append(make_trace_step(step_no, f"retry #{fb_index}", f"patch={patch}", "", ""))
            result = self._dispatch_action(action, patched_params)
            trace[-1]["observation"] = f"success={result.get('success')}"
            if result.get("success"):
                trace[-1]["decision"] = "done"
                task["execution_trace"] = trace
                return result
            trace[-1]["decision"] = "still_failing"

        task["failure_type"] = classify_failure(result.get("error", ""))
        task["execution_trace"] = trace
        return result

    def _dispatch_action(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """根据 action 分发到具体执行方法"""
        preconfirmed = params.get("_policy_preconfirmed", False)

        if action in (TerminalAction.SHELL, "execute_command", "shell"):
            return self.execute_shell(
                command=params.get("command", ""),
                working_dir=params.get("working_dir"),
                timeout=params.get("timeout"),
                env=params.get("env"),
                require_confirm=not preconfirmed,
            )
        elif action in (TerminalAction.CD, "cd"):
            return self.change_directory(params.get("path", params.get("working_dir", ".")))
        elif action in (TerminalAction.READ_FILE, "read_file"):
            return self.read_file(
                file_path=params.get("file_path", ""),
                offset=params.get("offset", 0),
                limit=params.get("limit"),
                encoding=params.get("encoding", "utf-8"),
            )
        elif action in (TerminalAction.WRITE_FILE, "write_file"):
            return self.write_file(
                file_path=params.get("file_path", ""),
                content=params.get("content", ""),
                encoding=params.get("encoding", "utf-8"),
                require_confirm=not preconfirmed,
            )
        elif action in (TerminalAction.EDIT_FILE, "edit_file"):
            return self.edit_file(
                file_path=params.get("file_path", ""),
                old_string=params.get("old_string", ""),
                new_string=params.get("new_string", ""),
                replace_all=params.get("replace_all", False),
                require_confirm=not preconfirmed,
            )
        elif action in (TerminalAction.GLOB, "glob"):
            return self.glob_search(
                pattern=params.get("pattern", "**/*"),
                path=params.get("path"),
                max_results=params.get("max_results", 100),
            )
        elif action in (TerminalAction.GREP, "grep"):
            return self.grep_search(
                pattern=params.get("pattern", ""),
                path=params.get("path"),
                include=params.get("include"),
                case_insensitive=params.get("case_insensitive", False),
                max_results=params.get("max_results", 50),
                context_lines=params.get("context_lines", 0),
            )
        elif action in (TerminalAction.LS, "ls"):
            return self.list_dir(
                path=params.get("path"),
                show_hidden=params.get("show_hidden", False),
            )
        else:
            return {"success": False, "error": f"未知终端操作: {action}"}

    def process(self, state: OmniCoreState) -> OmniCoreState:
        """LangGraph 节点函数"""
        from agents.paod import classify_failure

        for idx, task in enumerate(state["task_queue"]):
            if task["task_type"] == "terminal_worker" and task["status"] == "pending":
                state["task_queue"][idx]["status"] = "running"
                from core.message_bus import MessageBus
                bus = MessageBus.from_dict(state.get("message_bus", []))
                result = self.execute(task, bus.to_snapshot())
                state["task_queue"][idx]["status"] = "completed" if result.get("success") else "failed"
                state["task_queue"][idx]["result"] = result
                if not result.get("success"):
                    state["task_queue"][idx]["failure_type"] = (
                        task.get("failure_type") or classify_failure(result.get("error", ""))
                    )
                    state["error_trace"] = result.get("error", "未知错误")

        return state
