"""
OmniCore File Worker Agent
负责本地文件的读取、写入、创建操作
"""
import ast
import csv
import json
import os
import re
import zipfile
from typing import Dict, Any, Optional, List
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from core.state import OmniCoreState, TaskItem
from utils.logger import log_agent_action, logger, log_success, log_error, log_warning
from utils.human_confirm import HumanConfirm
from utils.time_context import (
    build_time_context_prompt,
    is_time_sensitive_text,
    normalize_current_time_context,
)
from config.settings import settings

def _import_paod():
    from agents.paod import classify_failure, make_trace_step
    return classify_failure, make_trace_step



class FileWorker:
    """
    文件操作 Worker Agent
    处理本地文件的读写操作，支持生成、追加、转换、压缩等多种模式。
    """

    def __init__(self):
        self.name = "FileWorker"

    # ------------------------------------------------------------------
    # 路径解析
    # ------------------------------------------------------------------

    @staticmethod
    def _looks_like_windows_drive_path(path_value: str) -> bool:
        return bool(re.match(r"^[a-zA-Z]:[\\/]", str(path_value or "").strip()))

    def _effective_desktop_path(self) -> Path:
        configured = Path(str(settings.USER_DESKTOP_PATH)).expanduser()
        if os.name != "nt" and self._looks_like_windows_drive_path(str(settings.USER_DESKTOP_PATH)):
            return Path.home() / "Desktop"
        return configured

    def _resolve_path(self, file_path: str) -> Path:
        """解析文件路径，支持 ~ 和相对路径"""
        raw_path = str(file_path or "").strip()
        path = Path(raw_path)

        if raw_path.startswith("~"):
            path = Path(raw_path).expanduser()

        if "Desktop" in str(path) or "桌面" in str(path):
            filename = path.name
            path = self._effective_desktop_path() / filename

        if not path.is_absolute() and str(path.parent) == ".":
            path = self._effective_desktop_path() / path.name

        return path

    # ------------------------------------------------------------------
    # 基础读写
    # ------------------------------------------------------------------

    def write_file(
        self,
        file_path: str,
        content: str,
        encoding: str = "utf-8",
        require_confirm: bool = True,
        policy_preconfirmed: bool = False,
    ) -> Dict[str, Any]:
        """覆盖写入文件（纯文本）"""
        path = self._resolve_path(file_path)
        log_agent_action(self.name, "准备写入文件", str(path))

        is_overwrite = path.exists()

        if settings.REQUIRE_HUMAN_CONFIRM and (require_confirm or not policy_preconfirmed):
            confirmed = HumanConfirm.request_file_write_confirmation(
                file_path=str(path),
                content_preview=content[:300],
                is_overwrite=is_overwrite,
            )
            if not confirmed:
                return {"success": False, "error": "用户取消操作", "file_path": str(path)}

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.write_text(content, encoding=encoding)
            except PermissionError:
                stem, suffix = path.stem, path.suffix
                timestamp = datetime.now().strftime("%H%M%S")
                path = path.parent / f"{stem}_{timestamp}{suffix}"
                path.write_text(content, encoding=encoding)
                log_warning(f"原文件被占用，已保存到: {path}")

            log_success(f"文件写入成功: {path}")
            return {"success": True, "file_path": str(path), "size": len(content), "encoding": encoding}

        except Exception as e:
            log_error(f"文件写入失败: {e}")
            return {"success": False, "error": str(e), "file_path": str(path)}

    def read_file(self, file_path: str, encoding: str = "utf-8") -> Dict[str, Any]:
        """读取文件"""
        path = self._resolve_path(file_path)
        log_agent_action(self.name, "读取文件", str(path))

        if not path.exists():
            return {"success": False, "error": f"文件不存在: {path}", "file_path": str(path)}

        try:
            content = path.read_text(encoding=encoding)
            return {"success": True, "file_path": str(path), "content": content, "size": len(content)}
        except Exception as e:
            log_error(f"文件读取失败: {e}")
            return {"success": False, "error": str(e), "file_path": str(path)}

    # ------------------------------------------------------------------
    # P1-1: 追加模式
    # ------------------------------------------------------------------

    def _append_file(self, file_path: str, content: str, fmt: str, data_items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """追加内容到已有文件末尾"""
        path = self._resolve_path(file_path)
        log_agent_action(self.name, "追加写入文件", str(path))

        try:
            path.parent.mkdir(parents=True, exist_ok=True)

            if fmt == "xlsx" and data_items:
                import openpyxl
                if path.exists():
                    wb = openpyxl.load_workbook(str(path))
                    ws = wb.active
                else:
                    wb = openpyxl.Workbook()
                    ws = wb.active
                    if data_items:
                        ws.append(list(data_items[0].keys()))
                for item in data_items:
                    ws.append(list(item.values()))
                wb.save(str(path))
                log_success(f"XLSX 追加成功: {path}")
                return {"success": True, "file_path": str(path), "format": "xlsx", "appended_rows": len(data_items)}

            elif fmt == "csv" and data_items:
                file_exists = path.exists()
                with open(str(path), "a", newline="", encoding="utf-8-sig") as f:
                    writer = csv.DictWriter(f, fieldnames=list(data_items[0].keys()))
                    if not file_exists:
                        writer.writeheader()
                    writer.writerows(data_items)
                log_success(f"CSV 追加成功: {path}")
                return {"success": True, "file_path": str(path), "format": "csv", "appended_rows": len(data_items)}

            else:
                # txt / markdown / html 等文本格式
                with open(str(path), "a", encoding="utf-8") as f:
                    if content and not content.endswith("\n"):
                        content += "\n"
                    f.write(content)
                log_success(f"文本追加成功: {path}")
                return {"success": True, "file_path": str(path), "format": fmt, "appended_bytes": len(content)}

        except Exception as e:
            log_error(f"追加写入失败: {e}")
            return {"success": False, "error": str(e), "file_path": str(path)}

    # ------------------------------------------------------------------
    # P0-1: LLM 驱动的文档内容生成
    # ------------------------------------------------------------------

    def _build_source_context(self, data_items: List[Dict[str, Any]], max_items: int = 12) -> str:
        """将抓取结果压缩成生成报告可用的事实材料。"""
        if not data_items:
            return ""

        compact_items = []
        for idx, item in enumerate(data_items[:max_items], 1):
            if not isinstance(item, dict):
                compact_items.append({"index": idx, "content": str(item)[:1200]})
                continue
            compact = {"index": idx}
            for key in (
                "title", "name", "summary", "snippet", "description", "text",
                "url", "link", "source", "source_url", "date", "published_at",
            ):
                value = item.get(key)
                if value:
                    compact[key] = str(value)[:1200]
            if len(compact) == 1:
                compact["content"] = json.dumps(item, ensure_ascii=False)[:1200]
            compact_items.append(compact)

        return json.dumps(compact_items, ensure_ascii=False, indent=2)

    def _extract_source_urls(self, data_items: List[Dict[str, Any]]) -> List[str]:
        urls: List[str] = []
        seen = set()
        for item in data_items:
            if not isinstance(item, dict):
                continue
            for key in ("url", "link", "source_url"):
                value = str(item.get(key, "") or "").strip()
                if value.startswith("http") and value not in seen:
                    seen.add(value)
                    urls.append(value)
        return urls

    def _ensure_html_document(self, content: str, title: str = "Generated Report") -> str:
        body = str(content or "").strip()
        if re.search(r"<html\b", body, flags=re.IGNORECASE):
            return body
        return (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>{title}</title>"
            "<style>"
            "body{font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
            "max-width:980px;margin:40px auto;padding:0 20px;line-height:1.65;color:#1f2933}"
            "a{color:#0b66c3} table{border-collapse:collapse;width:100%;margin:16px 0}"
            "th,td{border:1px solid #d9e2ec;padding:8px 10px;text-align:left;vertical-align:top}"
            "th{background:#f0f4f8} code{background:#f4f6f8;padding:1px 4px;border-radius:4px}"
            "</style></head><body>"
            f"{body}</body></html>"
        )

    def _append_missing_source_links(self, content: str, data_items: List[Dict[str, Any]], fmt: str) -> str:
        urls = self._extract_source_urls(data_items)
        if not urls:
            return content
        if any(url in content for url in urls):
            return content

        if fmt == "html":
            links = "".join(f"<li><a href=\"{url}\">{url}</a></li>" for url in urls)
            return f"{content}\n<section><h2>信息源</h2><ol>{links}</ol></section>"

        lines = ["", "## 信息源", *[f"- {url}" for url in urls]]
        return content.rstrip() + "\n" + "\n".join(lines) + "\n"

    def _ensure_generated_date(
        self,
        content: str,
        fmt: str,
        current_time_context: Dict[str, Any],
        *,
        force: bool = False,
    ) -> str:
        if not force:
            return content
        context = normalize_current_time_context(current_time_context)
        local_date = str(context.get("local_date", "") or "").strip()
        if not local_date or local_date in str(content or ""):
            return content
        if re.search(r"(报告生成日期|生成日期|generated(?:\s+at|\s+on)?)", str(content or ""), flags=re.IGNORECASE):
            return content

        if fmt == "html":
            return f"<p><strong>生成日期：</strong>{local_date}</p>\n{content}"
        if fmt in {"markdown", "md"}:
            return f"> 生成日期: {local_date}\n\n{content}"
        if fmt in {"txt", "text"}:
            return f"生成日期: {local_date}\n\n{content}"
        return content

    def _normalize_generated_file_path(self, file_path: str, fmt: str, explicit_file_path: bool) -> str:
        target = str(file_path or "").strip()
        normalized_fmt = str(fmt or "").strip().lower()
        if not target:
            suffix = "html" if normalized_fmt == "html" else "md"
            return f"~/Desktop/generated.{suffix}"
        if normalized_fmt == "html":
            suffix = Path(target).suffix.lower()
            if not suffix and not explicit_file_path:
                return f"{target}.html"
            if not explicit_file_path and suffix not in {".html", ".htm"}:
                return str(Path(target).with_suffix(".html"))
            if suffix in {".md", ".markdown", ".txt"} and Path(target).name.startswith("generated"):
                return str(Path(target).with_suffix(".html"))
        return target

    def _generate_content(
        self,
        topic: str,
        outline: List[str],
        style: str,
        fmt: str,
        source_context: str = "",
        current_time_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """调用 LLM 生成文档正文"""
        from core.llm import LLMClient
        from pathlib import Path as _Path

        prompt_path = _Path(__file__).parent.parent / "prompts" / "file_generate.txt"
        system_prompt = prompt_path.read_text(encoding="utf-8")

        outline_str = "、".join(outline) if outline else "（由 AI 自行规划章节）"
        system_prompt = (
            system_prompt
            .replace("{topic}", topic)
            .replace("{style}", style or "技术文档")
            .replace("{outline}", outline_str)
            .replace("{format}", fmt or "markdown")
        )
        time_context = normalize_current_time_context(current_time_context)
        system_prompt += (
            "\n\n## 当前时间（权威）\n"
            f"{build_time_context_prompt(time_context)}\n"
            "如果主题要求“最新/最近/当前/今天”，报告必须以该日期为基准，并明确生成日期。"
        )
        if source_context:
            system_prompt += (
                "\n\n## 已抓取的事实材料\n"
                "下面是上游 WebWorker 抓取/搜索到的材料。生成事实性报告时必须优先使用这些材料，"
                "不要编造未在材料中出现的发布时间、模型名或能力；如果材料不足，请明确说明。"
                "涉及来源时必须保留可点击 URL。\n"
                f"{source_context}\n"
            )

        llm = LLMClient()
        user_content = f"请根据以上要求，生成关于「{topic}」的{style or ''}文档。"
        if source_context:
            user_content += "请基于已抓取材料总结，并在文末列出信息源链接。"
        if is_time_sensitive_text(topic):
            user_content += f"当前日期是 {time_context.get('local_date')}，不要把更早年份的信息称为最新，除非来源证明仍然有效。"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        response = llm.chat(messages, temperature=0.7, max_tokens=settings.FILE_GENERATE_MAX_TOKENS)
        return response.content

    # ------------------------------------------------------------------
    # P0-2: Jinja2 模板渲染
    # ------------------------------------------------------------------

    def _render_template(self, template_name: str, data_items: List[Dict[str, Any]], title: str) -> str:
        """使用 Jinja2 渲染 templates/ 目录下的模板文件"""
        try:
            from jinja2 import Environment, FileSystemLoader, select_autoescape
        except ImportError:
            raise RuntimeError("jinja2 未安装，请运行 pip install jinja2")

        templates_dir = Path(__file__).parent.parent / "templates"
        env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            autoescape=select_autoescape(["html", "j2"]),
        )
        template = env.get_template(template_name)

        columns = list(data_items[0].keys()) if data_items else []
        context = {
            "title": title,
            "rows": data_items,
            "columns": columns,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "metadata": f"共 {len(data_items)} 条记录",
        }
        return template.render(**context)

    # ------------------------------------------------------------------
    # P1-2: 代码/配置文件格式验证
    # ------------------------------------------------------------------

    def _validate_code_content(self, content: str, fmt: str) -> str:
        """对生成的代码/配置内容做语法验证和格式化，返回处理后的内容"""
        if fmt == "python":
            try:
                ast.parse(content)
            except SyntaxError as e:
                log_warning(f"生成的 Python 代码存在语法错误: {e}，仍将写入文件")
            try:
                import black
                content = black.format_str(content, mode=black.Mode())
            except Exception:
                pass  # black 可选，格式化失败不阻断写入

        elif fmt == "json":
            try:
                parsed = json.loads(content)
                content = json.dumps(parsed, indent=2, ensure_ascii=False)
            except json.JSONDecodeError as e:
                log_warning(f"生成的 JSON 格式无效: {e}，仍将写入文件")

        elif fmt == "yaml":
            try:
                yaml.safe_load(content)
            except yaml.YAMLError as e:
                log_warning(f"生成的 YAML 格式无效: {e}，仍将写入文件")

        elif fmt == "toml":
            try:
                import tomllib
                tomllib.loads(content)
            except Exception:
                try:
                    import tomli
                    tomli.loads(content)
                except Exception as e:
                    log_warning(f"生成的 TOML 格式无效: {e}，仍将写入文件")

        return content

    # ------------------------------------------------------------------
    # P1-3: Artifact 元数据收集
    # ------------------------------------------------------------------

    def _build_artifact_preview(self, file_path: Path, data_items: List[Dict[str, Any]], fmt: str) -> str:
        """生成结构化的 artifact preview 字符串"""
        try:
            size_kb = round(file_path.stat().st_size / 1024, 1) if file_path.exists() else 0
            rows = len(data_items)
            cols = list(data_items[0].keys()) if data_items else []
            col_str = str(cols[:5])[1:-1]  # 最多展示前5列
            if len(cols) > 5:
                col_str += f", ...+{len(cols) - 5}列"
            return f"rows={rows}, cols=[{col_str}], format={fmt}, size={size_kb}KB"
        except Exception:
            return f"format={fmt}"

    # ------------------------------------------------------------------
    # P2-1: 流式写入大文件
    # ------------------------------------------------------------------

    def _write_csv_streaming(self, file_path: str, data_items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """流式写入 CSV（分批，避免 OOM）"""
        path = self._resolve_path(file_path)
        log_agent_action(self.name, f"流式写入 CSV（{len(data_items)} 行）", str(path))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            columns = list(data_items[0].keys()) if data_items else []
            chunk_size = settings.FILE_STREAM_CHUNK_SIZE
            with open(str(path), "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=columns)
                writer.writeheader()
                for i in range(0, len(data_items), chunk_size):
                    writer.writerows(data_items[i: i + chunk_size])
            log_success(f"流式 CSV 写入成功: {path}，共 {len(data_items)} 行")
            return {"success": True, "file_path": str(path), "format": "csv", "rows": len(data_items)}
        except Exception as e:
            log_error(f"流式 CSV 写入失败: {e}")
            return {"success": False, "error": str(e), "file_path": str(path)}

    # ------------------------------------------------------------------
    # P2-2: 列过滤
    # ------------------------------------------------------------------

    def _apply_column_filter(
        self,
        data_items: List[Dict[str, Any]],
        columns: Optional[List[str]],
        exclude_columns: Optional[List[str]],
    ) -> List[Dict[str, Any]]:
        """按白名单/黑名单过滤数据字段"""
        if not data_items:
            return data_items
        if columns:
            return [{k: item.get(k) for k in columns} for item in data_items]
        if exclude_columns:
            excl = set(exclude_columns)
            return [{k: v for k, v in item.items() if k not in excl} for item in data_items]
        return data_items

    # ------------------------------------------------------------------
    # P2-3: 格式转换
    # ------------------------------------------------------------------

    def _convert_file(self, source_path: str, target_path: str) -> Dict[str, Any]:
        """读取已有文件，转换格式后写出"""
        src = self._resolve_path(source_path)
        dst = self._resolve_path(target_path)
        log_agent_action(self.name, f"格式转换: {src.suffix} → {dst.suffix}", str(dst))

        if not src.exists():
            return {"success": False, "error": f"源文件不存在: {src}"}

        src_ext = src.suffix.lower().lstrip(".")
        dst_ext = dst.suffix.lower().lstrip(".")

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)

            # 读取源文件为 DataFrame
            if src_ext == "csv":
                df = pd.read_csv(str(src), encoding="utf-8-sig")
            elif src_ext in ("xlsx", "xls"):
                df = pd.read_excel(str(src))
            elif src_ext == "json":
                df = pd.read_json(str(src))
            else:
                return {"success": False, "error": f"不支持的源格式: {src_ext}"}

            # 写出目标格式
            if dst_ext == "csv":
                df.to_csv(str(dst), index=False, encoding="utf-8-sig")
            elif dst_ext == "xlsx":
                with pd.ExcelWriter(str(dst), engine="openpyxl") as w:
                    df.to_excel(w, index=False)
            elif dst_ext == "json":
                df.to_json(str(dst), orient="records", force_ascii=False, indent=2)
            elif dst_ext in ("md", "markdown"):
                dst.write_text(df.to_markdown(index=False), encoding="utf-8")
            else:
                return {"success": False, "error": f"不支持的目标格式: {dst_ext}"}

            log_success(f"格式转换成功: {dst}")
            return {"success": True, "file_path": str(dst), "rows": len(df), "format": dst_ext}

        except Exception as e:
            log_error(f"格式转换失败: {e}")
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # P2-4: 压缩打包
    # ------------------------------------------------------------------

    def _archive_files(self, sources: List[str], target_path: str) -> Dict[str, Any]:
        """将多个文件打包成 zip"""
        dst = self._resolve_path(target_path)
        log_agent_action(self.name, f"压缩打包 {len(sources)} 个文件", str(dst))

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            archived = []
            with zipfile.ZipFile(str(dst), "w", zipfile.ZIP_DEFLATED) as zf:
                for src_str in sources:
                    src = self._resolve_path(src_str)
                    if src.exists():
                        zf.write(str(src), arcname=src.name)
                        archived.append(src.name)
                    else:
                        log_warning(f"归档时跳过不存在的文件: {src}")

            log_success(f"压缩完成: {dst}，包含 {len(archived)} 个文件")
            return {"success": True, "file_path": str(dst), "archived": archived}

        except Exception as e:
            log_error(f"压缩打包失败: {e}")
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # 数据工具方法（原有，保持不变）
    # ------------------------------------------------------------------

    def format_data_to_text(self, data_items: List[Dict[str, Any]], title: str = "Data Report") -> str:
        """将数据格式化为可读文本（通用方法）"""
        lines = [
            "=" * 60,
            title,
            f"抓取时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 60,
            "",
        ]

        skip_fields = {"index", "title", "name", "link", "url", "id_link", "link_link", "title_link"}

        for idx, item in enumerate(data_items, 1):
            main_title = item.get("title", item.get("name", item.get("id", f"Item {idx}")))
            lines.append(f"{idx}. {main_title}")

            full_link = item.get("id_link", item.get("link_link", ""))
            if full_link and full_link.startswith("http"):
                lines.append(f"   链接: {full_link}")
            elif item.get("link", "").startswith("http"):
                lines.append(f"   链接: {item['link']}")
            elif item.get("url", "").startswith("http"):
                lines.append(f"   链接: {item['url']}")

            for key, value in item.items():
                if key not in skip_fields and value and key != "id":
                    display_key = {
                        "date": "日期", "severity": "危害等级", "description": "描述",
                        "author": "作者", "score": "评分", "points": "积分", "comments": "评论数",
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
        # 大数据自动切换流式写入
        if len(data_items) > settings.FILE_STREAM_THRESHOLD:
            return self._write_csv_streaming(file_path, data_items)
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
        """将数据写入 Markdown 文件"""
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
        """从多个 task_id 合并数据，每条数据标记 source 字段"""
        merged = []
        for source_id in data_sources:
            source_data = shared_memory.get(source_id)
            if isinstance(source_data, list):
                for item in source_data:
                    if isinstance(item, dict):
                        item_copy = dict(item)
                        item_copy.setdefault("source", source_id)
                        merged.append(item_copy)
            elif isinstance(source_data, dict) and isinstance(source_data.get("data"), list):
                for item in source_data.get("data") or []:
                    if isinstance(item, dict):
                        item_copy = dict(item)
                        item_copy.setdefault("source", source_id)
                        merged.append(item_copy)
            elif source_data is not None:
                merged.append({"data": str(source_data), "source": source_id})
        return merged

    def _generate_report_title(self, description: str) -> str:
        """根据任务描述智能生成报告标题"""
        import re
        title = description.strip()
        for prefix in ["将", "把", "保存", "写入", "生成", "导出", "创建"]:
            if title.startswith(prefix):
                title = title[len(prefix):]
        title = title.strip("，。、 ")
        if len(title) > 30:
            title = title[:30]
        return title or "数据报告"

    def _collect_data_items(self, params: Dict[str, Any], shared_memory: Dict[str, Any], task: TaskItem) -> List[Dict[str, Any]]:
        """从 shared_memory 收集数据项，支持单源和多源"""
        logger.debug(f"shared_memory keys: {list(shared_memory.keys())}")
        logger.debug(f"params data_source: {params.get('data_source')}, data_sources: {params.get('data_sources')}")

        data_sources = params.get("data_sources")
        if isinstance(data_sources, list) and data_sources:
            merged = self._merge_data_sources(data_sources, shared_memory)
            if merged:
                return merged

        data_source = params.get("data_source")
        if data_source and data_source in shared_memory:
            source_data = shared_memory[data_source]
            if isinstance(source_data, list) and source_data and isinstance(source_data[0], dict):
                return source_data
            elif isinstance(source_data, list) and source_data:
                return [{"data": str(item)} for item in source_data]
            elif isinstance(source_data, dict):
                nested_data = source_data.get("data")
                if isinstance(nested_data, list) and nested_data and isinstance(nested_data[0], dict):
                    return nested_data
                if isinstance(nested_data, list) and nested_data:
                    return [{"data": str(item)} for item in nested_data]
                return [{"data": json.dumps(source_data, ensure_ascii=False)}]

        if data_source:
            for key in shared_memory:
                if data_source in key or key in data_source:
                    value = shared_memory[key]
                    if isinstance(value, list) and value and isinstance(value[0], dict):
                        logger.debug(f"模糊匹配到数据源: {key}")
                        return value
                    if isinstance(value, dict):
                        nested_data = value.get("data")
                        if isinstance(nested_data, list) and nested_data and isinstance(nested_data[0], dict):
                            logger.debug(f"模糊匹配到嵌套数据源: {key}")
                            return nested_data

        for key, value in shared_memory.items():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                logger.debug(f"fallback 使用数据源: {key}")
                return value
            if isinstance(value, dict):
                nested_data = value.get("data")
                if isinstance(nested_data, list) and nested_data and isinstance(nested_data[0], dict):
                    logger.debug(f"fallback 使用嵌套数据源: {key}")
                    return nested_data

        logger.warning(f"未找到任何可用数据，shared_memory 内容: {list(shared_memory.keys())}")
        return []

    def _confirm_write_if_needed(
        self,
        task: TaskItem,
        file_path: str,
        data_items: List[Dict[str, Any]],
        preview_content: str = "",
    ) -> Optional[Dict[str, Any]]:
        if not task.get("requires_confirmation", False):
            return None

        preview = preview_content
        if not preview and data_items:
            preview = self.format_data_to_text(data_items[:3], "Preview")
        if not preview:
            preview = "Generated file content"

        resolved_path = str(self._resolve_path(file_path))
        confirmed = HumanConfirm.request_file_write_confirmation(
            file_path=resolved_path,
            content_preview=preview[:300],
            is_overwrite=Path(resolved_path).exists(),
        )
        if confirmed:
            return None
        return {"success": False, "error": "用户取消文件写入", "file_path": resolved_path}

    # ------------------------------------------------------------------
    # 主执行入口
    # ------------------------------------------------------------------

    def execute(self, task: TaskItem, shared_memory: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行文件操作任务（PAOD 增强：写入后硬验证）

        支持的 action：
          write   - 覆盖写入（原有）
          read    - 读取文件（原有）
          append  - P1-1 追加写入
          generate - P0-1 LLM 生成文档
          convert - P2-3 格式转换
          archive - P2-4 压缩打包
        """
        classify_failure, make_trace_step = _import_paod()

        params = task["params"]
        action = params.get("action", "")
        trace: List[Dict[str, Any]] = task.get("execution_trace", [])
        step_no = len(trace) + 1

        # 智能推断 action
        if not action:
            desc_lower = task["description"].lower()
            if params.get("data_source") or "save" in desc_lower or "write" in desc_lower or "保存" in desc_lower or "写入" in desc_lower:
                action = "write"
            else:
                action = "read"

        log_agent_action(self.name, f"执行任务: {action}", task["description"])

        # ---- P2-4: archive ----
        if action == "archive":
            sources = params.get("sources", [])
            target_path = params.get("target_path", params.get("file_path", "~/Desktop/archive.zip"))
            trace.append(make_trace_step(step_no, "archive files", target_path, "", ""))
            result = self._archive_files(sources, target_path)
            trace[-1]["observation"] = f"success={result.get('success')}, archived={result.get('archived', [])}"
            trace[-1]["decision"] = "done" if result.get("success") else "failed"
            if not result.get("success"):
                task["failure_type"] = classify_failure(result.get("error", ""))
            task["execution_trace"] = trace
            return result

        # ---- P2-3: convert ----
        if action == "convert":
            source_path = params.get("source_path", "")
            target_path = params.get("target_path", params.get("file_path", ""))
            trace.append(make_trace_step(step_no, "convert file", target_path, "", ""))
            result = self._convert_file(source_path, target_path)
            trace[-1]["observation"] = f"success={result.get('success')}, path={result.get('file_path', '')}"
            trace[-1]["decision"] = "done" if result.get("success") else "failed"
            if not result.get("success"):
                task["failure_type"] = classify_failure(result.get("error", ""))
            task["execution_trace"] = trace
            return result

        # ---- P0-1: generate ----
        if action == "generate":
            explicit_file_path = bool(str(params.get("file_path", "") or "").strip())
            requested_format = str(params.get("format", "") or "").strip().lower()
            file_path = params.get("file_path", "")
            topic = params.get("topic", task["description"])
            outline = params.get("outline", [])
            style = params.get("style", "技术文档")
            fmt = requested_format or Path(file_path).suffix.lower().lstrip(".") or "markdown"
            if fmt == "md":
                fmt = "markdown"
            file_path = self._normalize_generated_file_path(file_path, fmt, explicit_file_path)
            data_items = self._collect_data_items(params, shared_memory, task)
            data_items = self._apply_column_filter(
                data_items,
                params.get("columns"),
                params.get("exclude_columns"),
            )
            source_context = self._build_source_context(data_items)
            current_time_context = normalize_current_time_context(
                params.get("current_time_context")
                if isinstance(params.get("current_time_context"), dict)
                else (shared_memory or {}).get("current_time_context")
            )
            temporal_report = is_time_sensitive_text(f"{task.get('description', '')} {topic}")

            trace.append(make_trace_step(step_no, f"generate content ({fmt})", file_path, "", ""))
            try:
                content = self._generate_content(
                    topic,
                    outline,
                    style,
                    fmt,
                    source_context=source_context,
                    current_time_context=current_time_context,
                )
            except Exception as e:
                result = {"success": False, "error": f"LLM 生成失败: {e}", "file_path": file_path}
                trace[-1]["observation"] = f"error={e}"
                trace[-1]["decision"] = "failed"
                task["failure_type"] = classify_failure(str(e))
                task["execution_trace"] = trace
                return result

            # 代码/配置格式后处理（P1-2）
            code_fmts = {"python", "json", "yaml", "toml", "js", "javascript"}
            if fmt in code_fmts:
                content = self._validate_code_content(content, fmt)
            elif fmt == "html":
                content = self._ensure_generated_date(content, fmt, current_time_context, force=temporal_report)
                content = self._append_missing_source_links(content, data_items, fmt)
                content = self._ensure_html_document(content, title=self._generate_report_title(task["description"]))
            else:
                content = self._ensure_generated_date(content, fmt, current_time_context, force=temporal_report)
                content = self._append_missing_source_links(content, data_items, fmt)

            step_no += 1
            trace.append(make_trace_step(step_no, f"write generated file", file_path, "", ""))
            result = self.write_file(file_path, content, require_confirm=False, policy_preconfirmed=True)
            trace[-1]["observation"] = f"success={result.get('success')}, path={result.get('file_path', '')}"
            trace[-1]["decision"] = "done" if result.get("success") else "failed"
            if result.get("success") and data_items:
                actual_path = Path(result.get("file_path", ""))
                result["artifact_preview"] = self._build_artifact_preview(actual_path, data_items, fmt)

            # 硬验证
            step_no += 1
            trace.append(make_trace_step(step_no, "verify file", result.get("file_path", ""), "", ""))
            actual_path = Path(result.get("file_path", ""))
            if result.get("success") and actual_path.exists() and actual_path.stat().st_size > 0:
                trace[-1]["observation"] = f"exists=True, size={actual_path.stat().st_size}"
                trace[-1]["decision"] = "verified → done"
            else:
                trace[-1]["observation"] = f"exists={actual_path.exists()}"
                trace[-1]["decision"] = "verification_failed"
                result["success"] = False
                result["error"] = result.get("error", "文件验证失败：文件不存在或为空")
                task["failure_type"] = classify_failure(result.get("error", ""))

            task["execution_trace"] = trace
            return result

        # ---- P1-1: append ----
        if action == "append":
            file_path = params.get("file_path", "")
            if not file_path:
                file_path = "~/Desktop/output.txt"
            fmt = params.get("format", Path(file_path).suffix.lower().lstrip(".") or "txt")
            if fmt == "md":
                fmt = "markdown"

            data_items = self._collect_data_items(params, shared_memory, task)
            # P2-2: 列过滤
            data_items = self._apply_column_filter(
                data_items,
                params.get("columns"),
                params.get("exclude_columns"),
            )
            content = params.get("content", "")
            if not content and data_items:
                content = self.format_data_to_text(data_items, self._generate_report_title(task["description"]))

            trace.append(make_trace_step(step_no, f"append file ({fmt})", file_path, "", ""))
            result = self._append_file(file_path, content, fmt, data_items)
            trace[-1]["observation"] = f"success={result.get('success')}, path={result.get('file_path', '')}"
            trace[-1]["decision"] = "done" if result.get("success") else "failed"
            if not result.get("success"):
                task["failure_type"] = classify_failure(result.get("error", ""))
            task["execution_trace"] = trace
            return result

        # ---- read ----
        if action == "read":
            file_path = params.get("file_path", "")
            trace.append(make_trace_step(step_no, "read file", file_path, "", ""))
            result = self.read_file(file_path)
            trace[-1]["observation"] = f"success={result.get('success')}"
            trace[-1]["decision"] = "done" if result.get("success") else "failed"
            if not result.get("success"):
                task["failure_type"] = classify_failure(result.get("error", ""))
            task["execution_trace"] = trace
            return result

        # ---- write (default) ----
        if action == "write":
            file_path = params.get("file_path", "")
            user_preferences = shared_memory.get("user_preferences", {}) if isinstance(shared_memory, dict) else {}
            preferred_output_dir = ""
            if isinstance(user_preferences, dict):
                preferred_output_dir = str(user_preferences.get("default_output_directory", "") or "").strip()
            if not file_path:
                if preferred_output_dir:
                    file_path = str(Path(preferred_output_dir) / "output.txt")
                else:
                    file_path = "~/Desktop/output.txt"
            elif preferred_output_dir:
                candidate = Path(file_path)
                if not candidate.is_absolute() and str(candidate.parent) == ".":
                    file_path = str(Path(preferred_output_dir) / candidate.name)

            fmt = params.get("format", "")
            if not fmt:
                ext = Path(file_path).suffix.lower().lstrip(".")
                fmt = {"xlsx": "xlsx", "csv": "csv", "md": "markdown", "html": "html"}.get(ext, "txt")

            data_items = self._collect_data_items(params, shared_memory, task)
            # P2-2: 列过滤
            data_items = self._apply_column_filter(
                data_items,
                params.get("columns"),
                params.get("exclude_columns"),
            )
            report_title = self._generate_report_title(task["description"])

            # P0-2: Jinja2 模板渲染
            template_name = params.get("template", "")
            if template_name and data_items:
                trace.append(make_trace_step(step_no, f"render template ({template_name})", file_path, "", ""))
                try:
                    rendered = self._render_template(template_name, data_items, report_title)
                    result = self.write_file(file_path, rendered, require_confirm=False, policy_preconfirmed=True)
                except Exception as e:
                    result = {"success": False, "error": f"模板渲染失败: {e}", "file_path": file_path}
                trace[-1]["observation"] = f"success={result.get('success')}"
                task["execution_trace"] = trace
                return result

            preview_content = ""
            if not data_items:
                preview_content = params.get("content", "No data to write")

            cancel_result = self._confirm_write_if_needed(task, file_path, data_items, preview_content=preview_content)
            if cancel_result is not None:
                trace.append(make_trace_step(step_no, "confirm file write", file_path, "cancelled", "stop"))
                task["failure_type"] = classify_failure(cancel_result.get("error", ""))
                task["execution_trace"] = trace
                return cancel_result

            trace.append(make_trace_step(step_no, f"write file ({fmt})", file_path, "", ""))

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
                if data_items:
                    content = self.format_data_to_text(data_items, report_title)
                else:
                    content = params.get("content", "No data to write")
                result = self.write_file(file_path, content, require_confirm=False, policy_preconfirmed=True)

            trace[-1]["observation"] = f"success={result.get('success')}, path={result.get('file_path', '')}"

            # P1-3: 写入 artifact 元数据
            if result.get("success") and data_items:
                actual_path = Path(result.get("file_path", ""))
                result["artifact_preview"] = self._build_artifact_preview(actual_path, data_items, fmt)

            # 硬验证
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

        # 未知 action
        result = {"success": False, "error": f"未知操作类型: {action}"}
        task["failure_type"] = "invalid_input"
        task["execution_trace"] = trace
        return result

    def process(self, state: OmniCoreState) -> OmniCoreState:
        """LangGraph 节点函数：处理文件相关任务"""
        for idx, task in enumerate(state["task_queue"]):
            if task["task_type"] == "file_worker" and task["status"] == "pending":
                state["task_queue"][idx]["status"] = "running"

                from core.message_bus import MessageBus
                bus = MessageBus.from_dict(state.get("message_bus", []))
                result = self.execute(task, bus.to_snapshot())

                state["task_queue"][idx]["status"] = (
                    "completed" if result.get("success") else "failed"
                )
                state["task_queue"][idx]["result"] = result

                if not result.get("success"):
                    state["error_trace"] = result.get("error", "未知错误")

        return state
