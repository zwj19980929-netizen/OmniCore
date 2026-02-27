"""
OmniCore File Worker Agent
负责本地文件的读取、写入、创建操作
"""
import os
from typing import Dict, Any, Optional, List
from datetime import datetime
from pathlib import Path

import pandas as pd

from core.state import OmniCoreState, TaskItem
from utils.logger import log_agent_action, logger, log_success, log_error
from utils.human_confirm import HumanConfirm
from config.settings import settings

def _import_paod():
    from agents.paod import classify_failure, make_trace_step
    return classify_failure, make_trace_step


class FileWorker:
    """
    文件操作 Worker Agent
    处理本地文件的读写操作
    """

    def __init__(self):
        self.name = "FileWorker"

    def _resolve_path(self, file_path: str) -> Path:
        """解析文件路径，支持 ~ 和相对路径"""
        path = Path(file_path)

        # 处理 ~ 开头的路径
        if str(file_path).startswith("~"):
            path = Path(file_path).expanduser()

        # 处理桌面路径的特殊标记
        if "Desktop" in str(path) or "桌面" in str(path):
            filename = path.name
            path = settings.USER_DESKTOP_PATH / filename

        # 没有明确目录的文件（纯文件名），默认写到桌面
        if not path.is_absolute() and str(path.parent) == ".":
            path = settings.USER_DESKTOP_PATH / path.name

        return path

    def write_file(
        self,
        file_path: str,
        content: str,
        encoding: str = "utf-8",
        require_confirm: bool = True,
    ) -> Dict[str, Any]:
        """
        写入文件

        Args:
            file_path: 文件路径
            content: 文件内容
            encoding: 编码格式
            require_confirm: 是否需要人类确认

        Returns:
            操作结果
        """
        path = self._resolve_path(file_path)
        log_agent_action(self.name, "准备写入文件", str(path))

        # 检查是否为覆盖操作
        is_overwrite = path.exists()

        # 高危操作确认
        if require_confirm and settings.REQUIRE_HUMAN_CONFIRM:
            confirmed = HumanConfirm.request_file_write_confirmation(
                file_path=str(path),
                content_preview=content[:300],
                is_overwrite=is_overwrite,
            )
            if not confirmed:
                return {
                    "success": False,
                    "error": "用户取消操作",
                    "file_path": str(path),
                }

        try:
            # 确保父目录存在
            path.parent.mkdir(parents=True, exist_ok=True)

            # 写入文件（如果被占用则自动重命名）
            try:
                path.write_text(content, encoding=encoding)
            except PermissionError:
                # 文件可能被其他程序打开，尝试加时间戳重命名
                from datetime import datetime
                stem = path.stem
                suffix = path.suffix
                timestamp = datetime.now().strftime("%H%M%S")
                new_path = path.parent / f"{stem}_{timestamp}{suffix}"
                new_path.write_text(content, encoding=encoding)
                path = new_path
                log_warning(f"原文件被占用，已保存到: {path}")

            log_success(f"文件写入成功: {path}")
            return {
                "success": True,
                "file_path": str(path),
                "size": len(content),
                "encoding": encoding,
            }

        except Exception as e:
            log_error(f"文件写入失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "file_path": str(path),
            }

    def read_file(
        self,
        file_path: str,
        encoding: str = "utf-8",
    ) -> Dict[str, Any]:
        """
        读取文件

        Args:
            file_path: 文件路径
            encoding: 编码格式

        Returns:
            包含文件内容的结果
        """
        path = self._resolve_path(file_path)
        log_agent_action(self.name, "读取文件", str(path))

        if not path.exists():
            return {
                "success": False,
                "error": f"文件不存在: {path}",
                "file_path": str(path),
            }

        try:
            content = path.read_text(encoding=encoding)
            return {
                "success": True,
                "file_path": str(path),
                "content": content,
                "size": len(content),
            }
        except Exception as e:
            log_error(f"文件读取失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "file_path": str(path),
            }

    def format_data_to_text(self, data_items: List[Dict[str, Any]], title: str = "Data Report") -> str:
        """
        将数据格式化为可读文本（通用方法）

        Args:
            data_items: 数据列表 [{"title": "...", "link": "...", ...}]
            title: 报告标题

        Returns:
            格式化后的文本
        """
        lines = [
            "=" * 60,
            title,
            f"抓取时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 60,
            "",
        ]

        # 需要跳过的冗余字段
        skip_fields = {"index", "title", "name", "link", "url", "id_link", "link_link", "title_link"}

        for idx, item in enumerate(data_items, 1):
            # 获取主要字段
            main_title = item.get("title", item.get("name", item.get("id", f"Item {idx}")))

            # 优先使用详情链接
            link = item.get("id_link", item.get("link_link", item.get("link", item.get("url", ""))))
            # 确保链接是完整URL
            if link and not link.startswith("http"):
                link = ""  # 不完整的链接不显示，因为已经有完整的了

            lines.append(f"{idx}. {main_title}")

            # 显示完整链接
            full_link = item.get("id_link", item.get("link_link", ""))
            if full_link and full_link.startswith("http"):
                lines.append(f"   链接: {full_link}")
            elif item.get("link", "").startswith("http"):
                lines.append(f"   链接: {item['link']}")

            # 输出其他有意义的字段
            for key, value in item.items():
                if key not in skip_fields and value and key != "id":
                    # 美化字段名
                    display_key = {
                        "date": "日期",
                        "severity": "危害等级",
                        "description": "描述",
                        "author": "作者",
                        "score": "评分",
                        "points": "积分",
                        "comments": "评论数",
                    }.get(key, key)
                    lines.append(f"   {display_key}: {value}")

            lines.append("")

        lines.append("=" * 60)
        lines.append("Generated by OmniCore")

        return "\n".join(lines)

    def _write_excel(self, file_path: str, data_items: List[Dict[str, Any]], title: str = "Data Report") -> Dict[str, Any]:
        """将数据写入 Excel (.xlsx) 文件"""
        path = self._resolve_path(file_path)
        log_agent_action(self.name, "准备写入 Excel", str(path))

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            df = pd.DataFrame(data_items)
            try:
                with pd.ExcelWriter(str(path), engine="openpyxl") as writer:
                    df.to_excel(writer, sheet_name=title[:31], index=False)
            except PermissionError:
                timestamp = datetime.now().strftime("%H%M%S")
                new_path = path.parent / f"{path.stem}_{timestamp}{path.suffix}"
                with pd.ExcelWriter(str(new_path), engine="openpyxl") as writer:
                    df.to_excel(writer, sheet_name=title[:31], index=False)
                path = new_path
                log_warning(f"原文件被占用，已保存到: {path}")
            log_success(f"Excel 写入成功: {path}")
            return {"success": True, "file_path": str(path), "format": "xlsx", "rows": len(data_items)}
        except Exception as e:
            log_error(f"Excel 写入失败: {e}")
            return {"success": False, "error": str(e), "file_path": str(path)}

    def _write_csv(self, file_path: str, data_items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """将数据写入 CSV 文件（utf-8-sig 编码兼容 Excel 打开）"""
        path = self._resolve_path(file_path)
        log_agent_action(self.name, "准备写入 CSV", str(path))

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            df = pd.DataFrame(data_items)
            df.to_csv(str(path), index=False, encoding="utf-8-sig")
            log_success(f"CSV 写入成功: {path}")
            return {"success": True, "file_path": str(path), "format": "csv", "rows": len(data_items)}
        except Exception as e:
            log_error(f"CSV 写入失败: {e}")
            return {"success": False, "error": str(e), "file_path": str(path)}

    def _write_markdown(self, file_path: str, data_items: List[Dict[str, Any]], title: str = "Data Report") -> Dict[str, Any]:
        """将数据写入 Markdown 文件（使用 pandas to_markdown）"""
        path = self._resolve_path(file_path)
        log_agent_action(self.name, "准备写入 Markdown", str(path))

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            df = pd.DataFrame(data_items)
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            md_content = f"# {title}\n\n> 生成时间: {now}\n\n{df.to_markdown(index=False)}\n\n---\n*Generated by OmniCore*\n"
            path.write_text(md_content, encoding="utf-8")
            log_success(f"Markdown 写入成功: {path}")
            return {"success": True, "file_path": str(path), "format": "markdown", "rows": len(data_items)}
        except Exception as e:
            log_error(f"Markdown 写入失败: {e}")
            return {"success": False, "error": str(e), "file_path": str(path)}

    def _write_html(self, file_path: str, data_items: List[Dict[str, Any]], title: str = "Data Report") -> Dict[str, Any]:
        """生成带样式的 HTML 报告页面"""
        path = self._resolve_path(file_path)
        log_agent_action(self.name, "准备写入 HTML", str(path))

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            df = pd.DataFrame(data_items)
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            table_html = df.to_html(index=False, escape=False, classes="data-table")
            html_content = (
                f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
                f"<title>{title}</title><style>"
                f"body{{font-family:system-ui,sans-serif;max-width:960px;margin:40px auto;padding:0 20px;background:#f8f9fa}}"
                f"h1{{color:#1a1a2e}}p.meta{{color:#666;font-size:14px}}"
                f".data-table{{width:100%;border-collapse:collapse;margin:20px 0}}"
                f".data-table th{{background:#1a1a2e;color:#fff;padding:10px 12px;text-align:left}}"
                f".data-table td{{padding:8px 12px;border-bottom:1px solid #ddd}}"
                f".data-table tr:hover{{background:#e8f4f8}}"
                f"footer{{margin-top:30px;color:#999;font-size:12px}}"
                f"</style></head><body>"
                f"<h1>{title}</h1><p class='meta'>生成时间: {now}</p>"
                f"{table_html}"
                f"<footer>Generated by OmniCore</footer></body></html>"
            )
            path.write_text(html_content, encoding="utf-8")
            log_success(f"HTML 写入成功: {path}")
            return {"success": True, "file_path": str(path), "format": "html", "rows": len(data_items)}
        except Exception as e:
            log_error(f"HTML 写入失败: {e}")
            return {"success": False, "error": str(e), "file_path": str(path)}

    def _merge_data_sources(self, data_sources: List[str], shared_memory: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        从多个 task_id 合并数据，每条数据标记 source 字段。

        Args:
            data_sources: task_id 列表，如 ["task_1_jd", "task_2_taobao"]
            shared_memory: 共享内存

        Returns:
            合并后的数据列表
        """
        merged = []
        for source_id in data_sources:
            source_data = shared_memory.get(source_id)
            if isinstance(source_data, list):
                for item in source_data:
                    if isinstance(item, dict):
                        item_copy = dict(item)
                        item_copy.setdefault("source", source_id)
                        merged.append(item_copy)
            elif source_data is not None:
                merged.append({"data": str(source_data), "source": source_id})
        return merged

    def _generate_report_title(self, description: str) -> str:
        """根据任务描述智能生成报告标题"""
        # 取描述中最有意义的部分作为标题，去掉动词前缀
        import re
        title = description.strip()
        # 去掉常见的动作前缀
        for prefix in ["将", "把", "保存", "写入", "生成", "导出", "创建"]:
            if title.startswith(prefix):
                title = title[len(prefix):]
        # 截取合理长度
        title = title.strip("，。、 ")
        if len(title) > 30:
            title = title[:30]
        return title or "数据报告"

    def _collect_data_items(self, params: Dict[str, Any], shared_memory: Dict[str, Any], task: TaskItem) -> List[Dict[str, Any]]:
        """从 shared_memory 收集数据项，支持单源和多源"""
        logger.debug(f"shared_memory keys: {list(shared_memory.keys())}")
        logger.debug(f"params data_source: {params.get('data_source')}, data_sources: {params.get('data_sources')}")

        # 多数据源合并
        data_sources = params.get("data_sources")
        if isinstance(data_sources, list) and data_sources:
            merged = self._merge_data_sources(data_sources, shared_memory)
            if merged:
                return merged

        # 单数据源
        data_source = params.get("data_source")
        if data_source and data_source in shared_memory:
            source_data = shared_memory[data_source]
            if isinstance(source_data, list) and source_data and isinstance(source_data[0], dict):
                return source_data
            elif isinstance(source_data, list) and source_data:
                return [{"data": str(item)} for item in source_data]

        # 模糊匹配：data_source 可能是 "task_1" 但实际 key 是 "task_1_scrape" 之类
        if data_source:
            for key in shared_memory:
                if data_source in key or key in data_source:
                    value = shared_memory[key]
                    if isinstance(value, list) and value and isinstance(value[0], dict):
                        logger.debug(f"模糊匹配到数据源: {key}")
                        return value

        # fallback: 查找共享内存中的任何列表数据
        for key, value in shared_memory.items():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                logger.debug(f"fallback 使用数据源: {key}")
                return value

        logger.warning(f"未找到任何可用数据，shared_memory 内容: {list(shared_memory.keys())}")
        return []

    def execute(self, task: TaskItem, shared_memory: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行文件操作任务（PAOD 增强：写入后硬验证）

        Args:
            task: 任务项
            shared_memory: 共享内存（包含其他 Worker 的结果）

        Returns:
            执行结果
        """
        classify_failure, make_trace_step = _import_paod()

        params = task["params"]
        action = params.get("action", "")
        trace: List[Dict[str, Any]] = task.get("execution_trace", [])
        step_no = len(trace) + 1

        # 智能判断操作类型：如果有 data_source 或描述中包含"保存/写入/save/write"，则为写入操作
        if not action:
            desc_lower = task["description"].lower()
            if params.get("data_source") or "save" in desc_lower or "write" in desc_lower or "保存" in desc_lower or "写入" in desc_lower:
                action = "write"
            else:
                action = "read"

        log_agent_action(self.name, f"执行任务: {action}", task["description"])

        if action == "write":
            file_path = params.get("file_path", "")
            if not file_path:
                file_path = "~/Desktop/output.txt"

            # 确定输出格式：params["format"] > 文件扩展名推断 > 默认 txt
            fmt = params.get("format", "")
            if not fmt:
                ext = Path(file_path).suffix.lower().lstrip(".")
                fmt = {"xlsx": "xlsx", "csv": "csv", "md": "markdown", "html": "html"}.get(ext, "txt")

            # 收集数据项
            data_items = self._collect_data_items(params, shared_memory, task)
            report_title = self._generate_report_title(task["description"])

            # Step 1: 写入
            trace.append(make_trace_step(step_no, f"write file ({fmt})", file_path, "", ""))

            # 按格式分发到对应写入方法
            if data_items and fmt in ("xlsx", "csv", "markdown", "html"):
                if fmt == "xlsx":
                    result = self._write_excel(file_path, data_items, report_title)
                elif fmt == "csv":
                    result = self._write_csv(file_path, data_items)
                elif fmt == "markdown":
                    result = self._write_markdown(file_path, data_items, report_title)
                elif fmt == "html":
                    result = self._write_html(file_path, data_items, report_title)
            else:
                # fallback: txt 格式（向后兼容）
                if data_items:
                    content = self.format_data_to_text(data_items, report_title)
                else:
                    content = params.get("content", "No data to write")
                result = self.write_file(file_path, content, require_confirm=False)

            trace[-1]["observation"] = f"success={result.get('success')}, path={result.get('file_path', '')}"

            # Step 2: 硬验证 — 文件存在且非空
            step_no += 1
            trace.append(make_trace_step(step_no, "verify file", result.get("file_path", ""), "", ""))
            actual_path = Path(result.get("file_path", ""))
            if result.get("success") and actual_path.exists() and actual_path.stat().st_size > 0:
                trace[-1]["observation"] = f"exists=True, size={actual_path.stat().st_size}"
                trace[-1]["decision"] = "verified → done"
            else:
                trace[-1]["observation"] = f"exists={actual_path.exists()}, size={actual_path.stat().st_size if actual_path.exists() else 0}"
                trace[-1]["decision"] = "verification_failed"
                result["success"] = False
                result["error"] = result.get("error", "文件验证失败：文件不存在或为空")
                task["failure_type"] = classify_failure(result.get("error", ""))

            task["execution_trace"] = trace
            return result

        elif action == "read":
            file_path = params.get("file_path", "")
            trace.append(make_trace_step(step_no, "read file", file_path, "", ""))
            result = self.read_file(file_path)
            trace[-1]["observation"] = f"success={result.get('success')}"
            trace[-1]["decision"] = "done" if result.get("success") else "failed"
            if not result.get("success"):
                task["failure_type"] = classify_failure(result.get("error", ""))
            task["execution_trace"] = trace
            return result

        else:
            result = {"success": False, "error": f"未知操作类型: {action}"}
            task["failure_type"] = "invalid_input"
            task["execution_trace"] = trace
            return result

    def process(self, state: OmniCoreState) -> OmniCoreState:
        """
        LangGraph 节点函数：处理文件相关任务

        Args:
            state: 当前图状态

        Returns:
            更新后的状态
        """
        for idx, task in enumerate(state["task_queue"]):
            if task["task_type"] == "file_worker" and task["status"] == "pending":
                state["task_queue"][idx]["status"] = "running"

                result = self.execute(task, state["shared_memory"])

                state["task_queue"][idx]["status"] = (
                    "completed" if result.get("success") else "failed"
                )
                state["task_queue"][idx]["result"] = result

                # 存入共享内存
                state["shared_memory"][task["task_id"]] = result

                if not result.get("success"):
                    state["error_trace"] = result.get("error", "未知错误")

        return state
