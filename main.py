"""
OmniCore - 全栈智能体操作系统核心
主程序入口
"""
import os
import sys
import time
import traceback
from typing import List, Dict, Optional

from core.statuses import WAITING_JOB_STATUSES
from core.runtime import (
    get_background_worker_status,
    purge_session_working_memory,
    run_background_worker_forever,
    run_task,
    start_background_worker,
    stop_background_worker,
)
from memory.scoped_chroma_store import ChromaMemory
from utils.cli_result_view import build_cli_result_view
from utils.logger import console, log_error
from utils.enhanced_input import EnhancedInput

from rich.panel import Panel

# 全局 TerminalWorker 实例（在交互模式下跨会话持久化工作目录）
_terminal_worker: Optional[object] = None


def _get_terminal_worker():
    """懒加载 TerminalWorker（仅在 terminal 功能启用时）"""
    global _terminal_worker
    from config.settings import settings
    if not settings.TERMINAL_ENABLED:
        return None
    if _terminal_worker is None:
        from agents.terminal_worker import TerminalWorker
        _terminal_worker = TerminalWorker()
    return _terminal_worker


def _handle_skills_command(stripped: str) -> Dict:
    """Handle /skills [--all | deprecate <id> | delete <id>] command."""
    try:
        from memory.skill_store import SkillStore
        store = SkillStore()
    except Exception as e:
        return {"success": False, "output": f"Skill Library 初始化失败: {e}", "is_special_command": True}

    args = stripped[7:].strip()

    if args.startswith("deprecate "):
        skill_id = args[10:].strip()
        ok = store.deprecate_skill(skill_id)
        msg = f"已废弃: {skill_id}" if ok else f"未找到: {skill_id}"
        return {"success": ok, "output": msg, "is_special_command": True}

    if args.startswith("delete "):
        skill_id = args[7:].strip()
        ok = store.delete_skill(skill_id)
        msg = f"已删除: {skill_id}" if ok else f"未找到: {skill_id}"
        return {"success": ok, "output": msg, "is_special_command": True}

    include_deprecated = "--all" in args
    skills = store.list_skills(include_deprecated=include_deprecated)
    if not skills:
        return {"success": True, "output": "Skill Library 为空，完成任务后会自动提炼技能。", "is_special_command": True}

    lines = [f"Skill Library ({len(skills)} 个技能):", ""]
    for s in skills:
        status = "deprecated" if s.deprecated else "active"
        rate = f"{s.success_rate:.0%}" if s.total_uses > 0 else "N/A"
        lines.append(f"  [{status}] {s.name} (id={s.skill_id})")
        lines.append(f"         成功率={rate}  使用={s.total_uses}次  意图={s.source_intent}")
    lines.append("")
    lines.append("命令: /skills --all | /skills deprecate <id> | /skills delete <id>")
    return {"success": True, "output": "\n".join(lines), "is_special_command": True}


def _handle_learn_command(stripped: str) -> Dict:
    """Handle /learn <url_or_file_path> command."""
    target = stripped[6:].strip()
    if not target:
        return {"success": False, "output": "用法: /learn <url_or_file_path>", "is_special_command": True}

    try:
        from memory.knowledge_store import KnowledgeStore
        kb = KnowledgeStore()
    except Exception as e:
        return {"success": False, "output": f"知识库初始化失败: {e}", "is_special_command": True}

    if target.startswith("http://") or target.startswith("https://"):
        # URL — 通过 web_worker 抓取
        try:
            from agents.web_worker import WebWorker
            import asyncio
            worker = WebWorker()
            loop = asyncio.new_event_loop()
            result = loop.run_until_complete(
                worker.execute_async({"task": f"提取网页内容: {target}", "url": target}, {})
            )
            loop.close()
            content = str(result.get("extracted_text", "") or result.get("content", "") or "")
            title = str(result.get("title", "") or target)
            if not content:
                return {"success": False, "output": f"未能从 URL 提取内容: {target}", "is_special_command": True}
            count = kb.index_web_page(url=target, title=title, content=content)
            return {"success": True, "output": f"已索引网页: {title} ({count} chunks)", "is_special_command": True}
        except Exception as e:
            return {"success": False, "output": f"网页抓取失败: {e}", "is_special_command": True}
    else:
        # 本地文件
        file_path = os.path.expanduser(target)
        if not os.path.exists(file_path):
            return {"success": False, "output": f"文件不存在: {file_path}", "is_special_command": True}
        from utils.document_parser import extract_text
        content = extract_text(file_path)
        if not content:
            return {"success": False, "output": f"无法解析文件: {file_path}", "is_special_command": True}
        count = kb.index_document(file_path=file_path, content=content)
        return {"success": True, "output": f"已索引文件: {os.path.basename(file_path)} ({count} chunks)", "is_special_command": True}


def _handle_knowledge_command(stripped: str) -> Dict:
    """Handle /knowledge [search <query> | stats | delete <source>] command."""
    try:
        from memory.knowledge_store import KnowledgeStore
        kb = KnowledgeStore()
    except Exception as e:
        return {"success": False, "output": f"知识库初始化失败: {e}", "is_special_command": True}

    parts = stripped.split(None, 2)
    sub_cmd = parts[1] if len(parts) > 1 else "stats"

    if sub_cmd == "search" and len(parts) > 2:
        results = kb.search(parts[2])
        if not results:
            return {"success": True, "output": "未找到相关知识。", "is_special_command": True}
        lines = [f"搜索结果 ({len(results)} 条):", ""]
        for r in results:
            relevance = f"{1 - r.get('distance', 0):.0%}" if r.get("distance") is not None else "N/A"
            lines.append(f"  [{r.get('type', '')}] {r.get('title', 'N/A')} (相关度={relevance})")
            content_preview = r.get("content", "")[:200].replace("\n", " ")
            lines.append(f"    {content_preview}...")
        return {"success": True, "output": "\n".join(lines), "is_special_command": True}

    if sub_cmd == "delete" and len(parts) > 2:
        count = kb.delete_by_source(parts[2])
        return {"success": True, "output": f"已删除 {count} 条知识。", "is_special_command": True}

    # stats (default)
    stats = kb.get_stats()
    by_type = stats.get("by_type", {})
    total = stats.get("total_memories", 0)
    lines = [f"知识库统计 (共 {total} 条):", ""]
    for k, v in sorted(by_type.items()):
        lines.append(f"  {k}: {v} 条")
    if not by_type:
        lines.append("  (空)")
    lines.append("")
    lines.append("命令: /knowledge search <query> | /knowledge stats | /knowledge delete <source>")
    return {"success": True, "output": "\n".join(lines), "is_special_command": True}


def _handle_cost_command() -> Dict:
    """Handle /cost command — show current month LLM cost statistics."""
    from config.settings import settings
    try:
        from utils.cost_tracker import MonthlyCostGuard
        guard = MonthlyCostGuard(
            monthly_budget_usd=settings.MONTHLY_BUDGET_USD,
            data_dir=settings.DATA_DIR,
        )
    except Exception as e:
        return {"success": False, "output": f"成本追踪初始化失败: {e}", "is_special_command": True}

    used, budget, warning = guard.check_budget()
    models = guard.get_top_models_by_cost()
    month_tokens = guard.get_token_usage(period="month")
    day_tokens = guard.get_token_usage(period="day")

    budget_str = f"/ ${budget:.2f}" if budget > 0 else "(未设置预算)"
    warn_str = "  ⚠ 接近预算上限！" if warning else ""

    def _fmt_k(n: int) -> str:
        if n >= 1_000_000:
            return f"{n/1_000_000:.2f}M"
        if n >= 1_000:
            return f"{n/1_000:.1f}K"
        return str(n)

    lines = [f"本月 LLM 成本: ${used:.4f} {budget_str}{warn_str}", ""]
    lines.append(
        f"本月 token: in={_fmt_k(month_tokens['tokens_in'])} "
        f"out={_fmt_k(month_tokens['tokens_out'])} "
        f"total={_fmt_k(month_tokens['total'])} "
        f"({month_tokens['calls']} 次调用)"
    )
    lines.append(
        f"今日 token: in={_fmt_k(day_tokens['tokens_in'])} "
        f"out={_fmt_k(day_tokens['tokens_out'])} "
        f"total={_fmt_k(day_tokens['total'])} "
        f"({day_tokens['calls']} 次调用)"
    )
    lines.append("")
    if models:
        lines.append("按模型分布（成本 / token）：")
        for m in models[:8]:
            lines.append(
                f"  {m['model']}: ${m['cost_usd']:.4f} "
                f"({m['calls']} 次, in={_fmt_k(m.get('tokens_in', 0))} "
                f"out={_fmt_k(m.get('tokens_out', 0))})"
            )
    else:
        lines.append("暂无本月调用记录。")
    lines.append("")
    lines.append("设置月度预算：在 .env 中添加 MONTHLY_BUDGET_USD=<金额>")
    return {"success": True, "output": "\n".join(lines), "is_special_command": True}


def _handle_tool_health_command() -> Dict:
    """Handle /tool-health command — show per-tool failure profile (C2)."""
    from config.settings import settings as _settings
    if not getattr(_settings, "TOOL_FAILURE_PROFILE_ENABLED", False):
        return {
            "success": True,
            "output": "Tool failure profile 未启用 (设 TOOL_FAILURE_PROFILE_ENABLED=true 开启)。",
            "is_special_command": True,
        }
    try:
        from utils.tool_failure_profile import get_tool_failure_profile_store
        store = get_tool_failure_profile_store()
        if store is None:
            return {
                "success": True,
                "output": "Tool failure profile 未初始化。",
                "is_special_command": True,
            }
        profiles = store.get_all_profiles()
    except Exception as e:
        return {"success": False, "output": f"读取 tool 画像失败: {e}", "is_special_command": True}

    if not profiles:
        return {
            "success": True,
            "output": "尚无工具执行记录。运行一些任务后再试。",
            "is_special_command": True,
        }

    profiles.sort(key=lambda p: (p["fail_rate"], -p["total"]), reverse=True)
    lines = [f"工具失败画像 (window={_settings.TOOL_FAILURE_WINDOW})", ""]
    for p in profiles:
        top_tag = ""
        if p["error_tags"]:
            top_tag = max(p["error_tags"].items(), key=lambda kv: kv[1])[0]
        bits = [
            f"n={p['total']}",
            f"succ={p['success_rate']:.0%}",
            f"timeout={p['timeout_rate']:.0%}",
            f"avg={p['avg_latency_ms']}ms",
        ]
        if top_tag and p["fail_count"] > 0:
            bits.append(f"top_err={top_tag}")
        lines.append(f"  {p['tool_name']}: " + " | ".join(bits))

    hints = store.get_recommendations()
    if hints:
        lines.append("")
        lines.append("Planner 提示 (将注入 router):")
        for h in hints:
            lines.append(f"  [{h['level']}] {h['tool_name']} — {h['message']}")
    return {"success": True, "output": "\n".join(lines), "is_special_command": True}


def _handle_watch_command(stripped: str, session_id: str = "") -> Dict:
    """Handle /watch [url <url> | email | webhook | list | pause | resume | stop] command."""
    from config.settings import settings

    parts = stripped.split(maxsplit=3)
    sub_cmd = parts[1].lower() if len(parts) > 1 else "list"

    if sub_cmd == "url" and len(parts) >= 3:
        url = parts[2]
        note = parts[3] if len(parts) > 3 else ""
        from utils.event_sources.web_page_watch import WebPageWatchSource
        src = WebPageWatchSource(data_dir=str(settings.DATA_DIR))
        watch_id = src.create_watch({
            "url": url,
            "session_id": session_id,
            "note": note,
            "check_interval_seconds": settings.WEB_WATCH_DEFAULT_INTERVAL,
            "change_threshold": settings.WEB_WATCH_DEFAULT_THRESHOLD,
            "user_input_template": "网页 {url} 内容发生了变化，请抓取并汇报变化内容。",
        })
        lines = [
            f"已创建网页监控: {watch_id}",
            f"  URL: {url}",
            f"  检查间隔: {settings.WEB_WATCH_DEFAULT_INTERVAL}s",
            f"  变化阈值: {settings.WEB_WATCH_DEFAULT_THRESHOLD * 100:.0f}%",
        ]
        if note:
            lines.append(f"  备注: {note}")
        return {"success": True, "output": "\n".join(lines), "is_special_command": True}

    if sub_cmd == "list":
        lines = []
        # 网页监控
        try:
            from utils.event_sources.web_page_watch import WebPageWatchSource
            src = WebPageWatchSource(data_dir=str(settings.DATA_DIR))
            for w in src.list_watches():
                status_icon = {"active": "🟢", "paused": "⏸"}.get(w.get("status", ""), "⬜")
                lines.append(f"  {status_icon} [{w['watch_id']}] {w.get('url', 'N/A')} (间隔: {w.get('check_interval_seconds', '?')}s)")
        except Exception:
            pass
        # 邮件监控
        try:
            from utils.event_sources.email_watch import EmailWatchSource
            src = EmailWatchSource(data_dir=str(settings.DATA_DIR))
            for w in src.list_watches():
                status_icon = {"active": "🟢", "paused": "⏸"}.get(w.get("status", ""), "⬜")
                lines.append(f"  {status_icon} [{w['watch_id']}] {w.get('username', '')}@{w.get('imap_host', 'N/A')}")
        except Exception:
            pass
        # Webhook 监控
        try:
            from utils.event_sources.webhook_source import WebhookSource
            src = WebhookSource(data_dir=str(settings.DATA_DIR))
            for w in src.list_watches():
                status_icon = {"active": "🟢", "paused": "⏸"}.get(w.get("status", ""), "⬜")
                lines.append(f"  {status_icon} [{w['watch_id']}] webhook")
        except Exception:
            pass

        if not lines:
            lines.append("  暂无监控项。")
        lines.append("")
        lines.append("命令: /watch url <url> [备注] | /watch list | /watch pause <id> | /watch resume <id> | /watch stop <id>")
        return {"success": True, "output": "\n".join(lines), "is_special_command": True}

    if sub_cmd == "pause" and len(parts) >= 3:
        watch_id = parts[2]
        _watch_action(watch_id, "pause", settings)
        return {"success": True, "output": f"已暂停监控: {watch_id}", "is_special_command": True}

    if sub_cmd == "resume" and len(parts) >= 3:
        watch_id = parts[2]
        _watch_action(watch_id, "resume", settings)
        return {"success": True, "output": f"已恢复监控: {watch_id}", "is_special_command": True}

    if sub_cmd in ("stop", "delete") and len(parts) >= 3:
        watch_id = parts[2]
        _watch_action(watch_id, "delete", settings)
        return {"success": True, "output": f"已删除监控: {watch_id}", "is_special_command": True}

    return {
        "success": False,
        "output": "用法: /watch url <url> [备注] | /watch list | /watch pause <id> | /watch resume <id> | /watch stop <id>",
        "is_special_command": True,
    }


def _watch_action(watch_id: str, action: str, settings) -> None:
    """对指定 watch_id 执行 pause/resume/delete 操作。"""
    sources = []
    try:
        from utils.event_sources.web_page_watch import WebPageWatchSource
        sources.append(WebPageWatchSource(data_dir=str(settings.DATA_DIR)))
    except Exception:
        pass
    try:
        from utils.event_sources.email_watch import EmailWatchSource
        sources.append(EmailWatchSource(data_dir=str(settings.DATA_DIR)))
    except Exception:
        pass
    try:
        from utils.event_sources.webhook_source import WebhookSource
        sources.append(WebhookSource(data_dir=str(settings.DATA_DIR)))
    except Exception:
        pass

    for src in sources:
        try:
            getattr(src, f"{action}_watch")(watch_id)
        except Exception:
            pass


def _handle_builtin_command(user_input: str, session_id: str = "") -> Optional[Dict]:
    """
    处理终端内置快捷命令，返回 result dict 或 None（不是内置命令）。

    快捷命令：
      !<cmd>         直接执行 shell 命令（跳过 LLM 路由）
      /cd <path>     切换工作目录
      /ls [path]     列出目录
      /cwd           显示当前工作目录
      /allow <prefix> 会话内批准某类命令前缀
      /shell         显示当前 shell 和工作目录信息
      /cost          查看本月 LLM 成本统计
      /tool-health   查看工具失败画像 (C2)
      /watch         事件驱动监控管理
    """
    stripped = user_input.strip()

    # !cmd 快捷方式：直接执行 shell 命令
    if stripped.startswith("!"):
        cmd = stripped[1:].strip()
        if not cmd:
            return {"success": False, "output": "用法: !<命令>", "is_special_command": True}
        worker = _get_terminal_worker()
        if worker is None:
            return {"success": False, "output": "终端功能未启用（TERMINAL_ENABLED=false）", "is_special_command": True}

        console.print(f"[dim]$ {cmd}[/dim]")

        def _stream_cb(line: str, stream_type: str):
            if stream_type == "stdout":
                console.print(line, end="", highlight=False)
            else:
                console.print(f"[dim red]{line}[/dim red]", end="")

        result = worker.execute_shell(
            command=cmd,
            stream_callback=_stream_cb,
        )
        output = result.get("stdout", "") or result.get("error", "")
        return {
            "success": result.get("success", False),
            "output": output.strip(),
            "error": result.get("error", ""),
            "is_special_command": True,
            "status": "completed" if result.get("success") else "failed",
        }

    # /cd <path>
    if stripped.lower().startswith("/cd ") or stripped.lower() == "/cd":
        path = stripped[3:].strip() or "~"
        worker = _get_terminal_worker()
        if worker is None:
            return {"success": False, "output": "终端功能未启用", "is_special_command": True}
        result = worker.change_directory(path)
        if result.get("success"):
            return {"success": True, "output": f"工作目录: {result['working_dir']}", "is_special_command": True}
        return {"success": False, "output": result.get("error", "切换失败"), "is_special_command": True}

    # /ls [path]
    if stripped.lower().startswith("/ls"):
        path = stripped[3:].strip() or None
        worker = _get_terminal_worker()
        if worker is None:
            return {"success": False, "output": "终端功能未启用", "is_special_command": True}
        result = worker.list_dir(path=path)
        if result.get("success"):
            lines = []
            for entry in result["entries"]:
                icon = "📁" if entry["type"] == "dir" else "📄"
                size = f" ({entry['size']} B)" if entry.get("size") is not None else ""
                lines.append(f"  {icon} {entry['name']}{size}")
            return {
                "success": True,
                "output": f"{result['path']}\n" + "\n".join(lines),
                "is_special_command": True,
            }
        return {"success": False, "output": result.get("error", "列出失败"), "is_special_command": True}

    # /cwd
    if stripped.lower() == "/cwd":
        worker = _get_terminal_worker()
        cwd = worker.working_dir if worker else "终端功能未启用"
        return {"success": True, "output": f"当前工作目录: {cwd}", "is_special_command": True}

    # /allow <prefix>
    if stripped.lower().startswith("/allow "):
        prefix = stripped[7:].strip()
        worker = _get_terminal_worker()
        if worker is None:
            return {"success": False, "output": "终端功能未启用", "is_special_command": True}
        worker.approve_command_prefix(prefix)
        return {"success": True, "output": f"已批准命令前缀: '{prefix}'（本会话内有效）", "is_special_command": True}

    # /watch [url|list|pause|resume|stop]
    if stripped.lower().startswith("/watch"):
        return _handle_watch_command(stripped, session_id=session_id)

    # /skills [--all]
    if stripped.lower().startswith("/skills"):
        return _handle_skills_command(stripped)

    # /learn <url_or_file>
    if stripped.lower().startswith("/learn"):
        return _handle_learn_command(stripped)

    # /knowledge [search|stats|delete]
    if stripped.lower().startswith("/knowledge"):
        return _handle_knowledge_command(stripped)

    # /cost — 本月 LLM 成本统计
    if stripped.lower() == "/cost":
        return _handle_cost_command()

    # /tool-health — 工具失败画像（C2）
    if stripped.lower() in ("/tool-health", "/toolhealth"):
        return _handle_tool_health_command()

    # /shell
    if stripped.lower() == "/shell":
        from config.settings import settings
        import platform
        worker = _get_terminal_worker()
        info = [
            f"OS:          {platform.system()} {platform.release()} ({platform.machine()})",
            f"Shell:       {worker.shell if worker else settings.TERMINAL_SHELL}",
            f"工作目录:    {worker.working_dir if worker else 'N/A'}",
            f"权限模式:    {settings.TERMINAL_PERMISSION_MODE}",
            f"默认超时:    {settings.TERMINAL_DEFAULT_TIMEOUT}s",
            f"沙箱模式:    {'启用 → ' + settings.TERMINAL_SANDBOX_ROOT if settings.TERMINAL_SANDBOX_ENABLED else '禁用'}",
            f"流式输出:    {'启用' if settings.TERMINAL_STREAM_OUTPUT else '禁用'}",
        ]
        return {"success": True, "output": "\n".join(info), "is_special_command": True}

    return None  # 不是内置命令


def _handle_attach_command(raw_input: str) -> Optional[str]:
    """
    处理 /attach <file_path> [文字描述] 命令。

    Returns:
        处理后的文本 user_input，失败时返回 None。
    """
    parts = raw_input[7:].strip().split(None, 1)
    if not parts:
        console.print("[yellow]用法: /attach <file_path> [描述][/yellow]")
        return None

    file_path = os.path.expanduser(parts[0])
    text_hint = parts[1] if len(parts) > 1 else ""

    if not os.path.exists(file_path):
        console.print(f"[red]文件不存在: {file_path}[/red]")
        return None

    from utils.multimodal_input import (
        MultimodalInputProcessor,
        build_multimodal_input,
    )

    inp = build_multimodal_input(file_path, text_hint)
    if inp is None:
        ext = os.path.splitext(file_path)[1].lower()
        console.print(f"[yellow]不支持的文件类型: {ext}[/yellow]")
        return None

    console.print(f"[dim]正在处理附件: {os.path.basename(file_path)}...[/dim]")
    processor = MultimodalInputProcessor()
    processed = processor.process(inp)
    if processed:
        preview = processed[:120].replace("\n", " ")
        console.print(f"[dim]已解析: {preview}{'...' if len(processed) > 120 else ''}[/dim]")
    else:
        console.print("[yellow]附件处理未产生有效文本[/yellow]")
    return processed or None


def print_banner():
    """打印启动横幅"""
    banner = r"""
   ____                  _ ______
  / __ \____ ___  ____  (_) ____/___  ________
 / / / / __ `__ \/ __ \/ / /   / __ \/ ___/ _ \
/ /_/ / / / / / / / / / / /___/ /_/ / /  /  __/
\____/_/ /_/ /_/_/ /_/_/\____/\____/_/   \___/

    Full-Stack Agentic OS Core v0.1.0
    """
    console.print(Panel(banner, style="cyan", title="OmniCore"))


def _purge_working_memory_on_exit(session_id: Optional[str]) -> None:
    """A4: best-effort cleanup of working-tier memory at CLI session exit."""
    if not session_id:
        return
    try:
        deleted = purge_session_working_memory(session_id)
        if deleted:
            console.print(f"[dim]Cleared {deleted} working-tier memories for session {session_id}.[/dim]")
    except Exception:
        pass


def interactive_mode():
    """交互式命令行模式 - 支持历史记录和优雅退出"""
    print_banner()

    # 初始化记忆系统（同步预热，fd 重定向抑制 safetensors 的 LOAD REPORT）
    # 在进入交互循环之前完成，此时没有 input() 竞争，fd 重定向安全。
    try:
        import os
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
        os.environ["HF_HUB_VERBOSITY"] = "error"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"

        memory = ChromaMemory(silent=True)
        memory._collection  # 同步触发模型加载 + ChromaDB 初始化
        console.print("[dim]记忆系统就绪[/dim]\n")
    except Exception as e:
        console.print(f"[yellow]记忆系统初始化失败: {e}[/yellow]\n")
        memory = None

    console.print("[green]输入你的指令，按 Ctrl+C 或 Ctrl+D 退出[/green]")
    console.print("[dim]提示：使用上下方向键浏览历史命令 | !<cmd> 直接执行 shell | /cd /ls /cwd /allow /shell /attach /skills /learn /knowledge /watch[/dim]\n")

    # 对话上下文：保留最近 5 轮的交互记录
    conversation_history: List[Dict] = []
    MAX_HISTORY = 5
    session_id = None

    # 初始化增强输入
    enhanced_input = EnhancedInput()

    if not enhanced_input.has_readline and sys.platform == "win32":
        console.print("[dim]Windows 下默认禁用 pyreadline3，避免交互式输入因控制台尺寸探测失败而崩溃。[/dim]")
        console.print("[dim]如需强制启用，可设置环境变量 OMNICORE_ENABLE_PYREADLINE3=1。[/dim]\n")
    elif not enhanced_input.has_readline:
        console.print("[dim]提示：安装 gnureadline 可获得更好的命令行体验[/dim]")
        console.print("[dim]  macOS: pip install gnureadline[/dim]\n")

    while True:
        try:
            # prompt 不使用 emoji，避免 readline/libedit 光标错位
            console.print()
            user_input = enhanced_input.input("OmniCore > ")

            if user_input.lower() in ["quit", "exit", "q"]:
                console.print("\n[yellow]再见！👋[/yellow]")
                _purge_working_memory_on_exit(session_id)
                break

            # 终端内置快捷命令（!cmd, /cd, /ls, /cwd, /allow, /shell）
            builtin_result = _handle_builtin_command(user_input, session_id=session_id or "")
            if builtin_result is not None:
                if not builtin_result.get("is_special_command") or user_input.strip().startswith("!"):
                    # 对于 !cmd，流式输出已打印，只显示状态
                    status_style = "green" if builtin_result.get("success") else "red"
                    if not builtin_result.get("success") and builtin_result.get("output"):
                        console.print(f"[{status_style}]{builtin_result['output']}[/{status_style}]")
                else:
                    output = builtin_result.get("output", "")
                    style = "green" if builtin_result.get("success") else "red"
                    if output:
                        console.print(f"[{style}]{output}[/{style}]")
                continue

            # 内置命令：查看历史
            if user_input.lower() == "history":
                history = enhanced_input.get_history(20)
                if history:
                    console.print("\n[cyan]最近的命令：[/cyan]")
                    for i, cmd in enumerate(history, 1):
                        console.print(f"  [dim]{i}.[/dim] {cmd}")
                else:
                    console.print("[dim]暂无历史记录[/dim]")
                continue

            # 内置命令：清除历史
            if user_input.lower() == "clear history":
                enhanced_input.clear_history()
                console.print("[green]历史记录已清除[/green]")
                continue

            # /attach <file_path> [可选文字描述]
            if user_input.strip().startswith("/attach"):
                processed = _handle_attach_command(user_input)
                if processed:
                    user_input = processed
                else:
                    continue

            if not user_input.strip():
                continue

            # 执行任务，传入对话历史
            result = run_task(
                user_input,
                memory,
                conversation_history,
                session_id=session_id,
            )
            session_id = result.get("session_id") or session_id

            # 非内置命令才加入对话历史，避免污染 Router 上下文
            if not result.get("is_special_command"):
                turn_record = {
                    "user_input": user_input,
                    "success": result.get("success", False),
                    "output": (result.get("output") or result.get("error") or "")[:300],
                }
                conversation_history.append(turn_record)
                if len(conversation_history) > MAX_HISTORY:
                    conversation_history.pop(0)

            # 显示结果
            console.print()
            view = build_cli_result_view(result)
            console.print(Panel(
                view["body"],
                title=view["title"],
                border_style=view["border_style"],
            ))

        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]再见！[/yellow]")
            _purge_working_memory_on_exit(session_id)
            break
        except Exception as e:
            error_detail = traceback.format_exc()
            log_error(f"发生错误: {e}")
            console.print(f"[dim]{error_detail}[/dim]")
            continue

    # 保存历史记录
    enhanced_input.save_history()


def worker_mode():
    """Run the queue worker as a dedicated foreground process."""
    print_banner()
    started = start_background_worker()
    status = get_background_worker_status()
    console.print(f"[green]Queue worker {'started' if started else 'already running'}[/green]")
    if status.get("persisted"):
        console.print(f"[dim]{status['persisted']}[/dim]")
    console.print("[green]Press Ctrl+C to stop the worker.[/green]")

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        stopped = stop_background_worker()
        if stopped:
            console.print("[yellow]Queue worker stopped[/yellow]")
        else:
            console.print("[yellow]Queue worker was not running[/yellow]")


def _run_preference_learn(dry_run: bool = False) -> None:
    """A5: one-shot preference learning entrypoint."""
    from memory.preference_learner import infer_preferences
    from memory.manager import MemoryManager
    memory = ChromaMemory(silent=True)
    candidates = infer_preferences(memory)
    if not candidates:
        console.print("[yellow]Preference learner: no candidates above threshold.[/yellow]")
        return
    console.print(f"[green]Preference learner: {len(candidates)} candidates[/green]")
    for cand in candidates:
        console.print(
            f"  · {cand.key}={cand.value}  conf={cand.confidence:.2f}  {cand.notes}"
        )
    if dry_run:
        console.print("[dim](dry-run — not persisted)[/dim]")
        return
    manager = MemoryManager(chroma_memory=memory)
    written = manager.persist_inferred_preferences(candidates)
    console.print(f"[green]Persisted {len(written)} preferences.[/green]")


def _run_memory_consolidate(dry_run: bool = False) -> None:
    """A1: one-shot memory consolidation entrypoint."""
    from memory.consolidator import consolidate_expired
    memory = ChromaMemory(silent=True)
    report = consolidate_expired(memory, dry_run=dry_run)
    data = report.as_dict()
    tag = "[dry-run] " if dry_run else ""
    console.print(f"[green]{tag}Memory consolidation:[/green]")
    console.print(
        f"  scanned={data['scanned']}  "
        f"deleted_never_used={data['deleted_never_used']}  "
        f"consolidated_groups={data['consolidated_groups']}  "
        f"consolidated_memories={data['consolidated_memories']}  "
        f"skipped_diverse={data['skipped_diverse']}  "
        f"errors={data['errors']}"
    )
    for detail in data.get("details", [])[:10]:
        console.print(f"  · [{detail['scope_key']}] x{detail['archived_count']}: {detail['summary_preview']}")


def main():
    if len(sys.argv) >= 3 and sys.argv[1].lower() == "worker" and sys.argv[2] == "--process-loop":
        run_background_worker_forever()
        return
    if len(sys.argv) == 2 and sys.argv[1].lower() == "worker":
        worker_mode()
        return
    if len(sys.argv) >= 2 and sys.argv[1].lower() == "memory-consolidate":
        dry = "--dry-run" in sys.argv[2:]
        _run_memory_consolidate(dry_run=dry)
        return
    if len(sys.argv) >= 2 and sys.argv[1].lower() == "preference-learn":
        dry = "--dry-run" in sys.argv[2:]
        _run_preference_learn(dry_run=dry)
        return
    """主函数"""
    if len(sys.argv) > 1:
        # 命令行参数模式
        user_input = " ".join(sys.argv[1:])
        result = run_task(user_input)
        status = str(result.get("status", "") or "")
        view = build_cli_result_view(result)
        if result["success"] or status in WAITING_JOB_STATUSES:
            print(view["body"])
        else:
            print(f"Error: {view['body']}")
            sys.exit(1)
    else:
        # 交互式模式
        interactive_mode()


if __name__ == "__main__":
    main()
