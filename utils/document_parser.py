"""
文档文本提取工具。

支持格式：PDF、DOCX、TXT、Markdown。
外部依赖（可选）：PyPDF2、python-docx。缺失时对应格式返回空字符串。
"""

from pathlib import Path
from typing import Union

from utils.logger import log_warning


def extract_text(file_path: Union[str, Path]) -> str:
    """
    从文件中提取纯文本内容。

    Args:
        file_path: 文件路径

    Returns:
        提取的文本内容；格式不支持或解析失败时返回空字符串。
    """
    path = Path(file_path)
    if not path.exists():
        log_warning(f"document_parser: file not found: {path}")
        return ""

    ext = path.suffix.lower()

    if ext in (".txt", ".md", ".markdown", ".csv", ".json", ".yaml", ".yml"):
        return _extract_plain_text(path)
    if ext == ".pdf":
        return _extract_pdf(path)
    if ext == ".docx":
        return _extract_docx(path)

    log_warning(f"document_parser: unsupported format: {ext}")
    return ""


def _extract_plain_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="gbk")
        except Exception as e:
            log_warning(f"document_parser: plain text read failed: {e}")
            return ""


def _extract_pdf(path: Path) -> str:
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        log_warning("document_parser: PyPDF2 not installed, PDF extraction skipped")
        return ""

    try:
        reader = PdfReader(str(path))
        pages = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
        return "\n\n".join(pages)
    except Exception as e:
        log_warning(f"document_parser: PDF extraction failed: {e}")
        return ""


def _extract_docx(path: Path) -> str:
    try:
        from docx import Document
    except ImportError:
        log_warning("document_parser: python-docx not installed, DOCX extraction skipped")
        return ""

    try:
        doc = Document(str(path))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(paragraphs)
    except Exception as e:
        log_warning(f"document_parser: DOCX extraction failed: {e}")
        return ""
