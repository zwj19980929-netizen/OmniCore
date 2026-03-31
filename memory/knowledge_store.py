"""
KnowledgeStore — 个人知识库的存储与检索。

基于 ChromaMemory，使用独立 collection ``omnicore_knowledge``，
按 memory_type 区分内容类型：

- ``web_page``:  浏览器 / 爬虫抓取的网页内容
- ``document``:  用户主动导入的文档（PDF / TXT / MD / DOCX）
- ``task_result``: 任务执行的关键结果摘要
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from config.settings import settings
from memory.scoped_chroma_store import ChromaMemory
from utils.logger import log_agent_action, log_warning


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class KnowledgeItem:
    """知识条目（仅用于入参描述，不做持久化）。"""
    content: str
    source_type: str            # web_page | document | task_result
    source_url: str = ""
    title: str = ""
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# KnowledgeStore
# ---------------------------------------------------------------------------

class KnowledgeStore:
    """个人知识库管理 — 索引 / 检索 / 管理。"""

    CHUNK_SIZE = 1000       # 字符
    CHUNK_OVERLAP = 200     # 重叠字符
    COLLECTION_NAME = "omnicore_knowledge"

    def __init__(self) -> None:
        self._store = ChromaMemory(collection_name=self.COLLECTION_NAME)

    # ── 索引 ──────────────────────────────────────────────────────────────

    def index_web_page(
        self,
        url: str,
        title: str,
        content: str,
        *,
        session_id: str = "",
        job_id: str = "",
    ) -> int:
        """索引网页内容，返回写入的 chunk 数。"""
        if not settings.KNOWLEDGE_BASE_ENABLED:
            return 0
        chunks = self._split_text(content)
        if not chunks:
            return 0

        fingerprint_base = hashlib.md5(url.encode()).hexdigest()[:12]
        now = datetime.now().isoformat(timespec="seconds")

        for i, chunk in enumerate(chunks):
            fp = f"kb:web:{fingerprint_base}:{i}"
            self._store.add_memory(
                content=chunk,
                metadata={
                    "source_url": url,
                    "title": title,
                    "chunk_index": str(i),
                    "total_chunks": str(len(chunks)),
                    "session_id": session_id,
                    "job_id": job_id,
                    "indexed_at": now,
                },
                memory_type="web_page",
                fingerprint=fp,
                allow_update=True,
                skip_dedup=True,
            )

        log_agent_action("KnowledgeStore", "Indexed web page", f"{title} ({len(chunks)} chunks)")
        return len(chunks)

    def index_document(
        self,
        file_path: str,
        content: str,
        title: str = "",
    ) -> int:
        """索引文档文件，返回写入的 chunk 数。"""
        if not settings.KNOWLEDGE_BASE_ENABLED:
            return 0
        chunks = self._split_text(content)
        if not chunks:
            return 0

        fingerprint_base = hashlib.md5(file_path.encode()).hexdigest()[:12]
        if not title:
            title = file_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        now = datetime.now().isoformat(timespec="seconds")

        for i, chunk in enumerate(chunks):
            fp = f"kb:doc:{fingerprint_base}:{i}"
            self._store.add_memory(
                content=chunk,
                metadata={
                    "source_path": file_path,
                    "title": title,
                    "chunk_index": str(i),
                    "total_chunks": str(len(chunks)),
                    "indexed_at": now,
                },
                memory_type="document",
                fingerprint=fp,
                allow_update=True,
                skip_dedup=True,
            )

        log_agent_action("KnowledgeStore", "Indexed document", f"{title} ({len(chunks)} chunks)")
        return len(chunks)

    def index_task_result(
        self,
        summary: str,
        user_input: str,
        job_id: str,
        *,
        session_id: str = "",
    ) -> bool:
        """索引任务结果摘要。返回是否成功写入。"""
        if not settings.KNOWLEDGE_BASE_ENABLED:
            return False
        min_len = settings.KNOWLEDGE_MIN_CONTENT_LENGTH
        if len(summary) < min_len:
            return False

        fp = f"kb:task:{job_id}"
        now = datetime.now().isoformat(timespec="seconds")
        mid = self._store.add_memory(
            content=summary,
            metadata={
                "user_input": user_input[:200],
                "job_id": job_id,
                "session_id": session_id,
                "indexed_at": now,
            },
            memory_type="task_result",
            fingerprint=fp,
            allow_update=True,
            skip_dedup=True,
        )
        return bool(mid)

    # ── 检索（RAG）────────────────────────────────────────────────────────

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 0,
        source_types: Optional[List[str]] = None,
        max_total_chars: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        语义检索相关知识。

        Args:
            query: 检索查询文本
            top_k: 每个 memory_type 的返回数，0 使用配置默认值
            source_types: 限定来源类型，None 表示全部
            max_total_chars: 返回文本总长度上限，0 使用配置默认值

        Returns:
            按相关性排序的知识条目列表
        """
        if not query:
            return []

        effective_top_k = top_k or settings.KNOWLEDGE_RETRIEVAL_TOP_K
        effective_max_chars = max_total_chars or settings.KNOWLEDGE_MAX_CONTEXT_CHARS
        distance_threshold = settings.KNOWLEDGE_DISTANCE_THRESHOLD
        types_to_search = source_types or ["web_page", "document", "task_result"]

        all_results: List[Dict[str, Any]] = []

        for memory_type in types_to_search:
            try:
                items = self._store.search_memory(
                    query=query,
                    n_results=effective_top_k,
                    memory_type=memory_type,
                    include_global_fallback=True,
                )
                for item in items:
                    dist = item.get("distance")
                    if dist is not None and dist > distance_threshold:
                        continue
                    meta = item.get("metadata", {})
                    all_results.append({
                        "content": item.get("content", ""),
                        "source": (
                            meta.get("source_url")
                            or meta.get("source_path")
                            or meta.get("job_id", "")
                        ),
                        "title": meta.get("title", ""),
                        "distance": dist,
                        "type": memory_type,
                    })
            except Exception as exc:
                log_warning(f"Knowledge retrieval failed for {memory_type}: {exc}")

        # 按距离排序（越小越相关）
        all_results.sort(key=lambda x: x.get("distance") or 999)

        # 截断到 max_total_chars
        selected: List[Dict[str, Any]] = []
        total_chars = 0
        for r in all_results:
            content_len = len(r.get("content", ""))
            if total_chars + content_len > effective_max_chars:
                break
            selected.append(r)
            total_chars += content_len

        return selected

    def format_as_context(self, results: List[Dict[str, Any]]) -> str:
        """将检索结果格式化为 LLM 上下文注入段落。"""
        if not results:
            return ""

        lines = ["## 相关知识（来自知识库）\n"]
        for r in results:
            source_label = r.get("title") or r.get("source") or "unknown"
            lines.append(f"### [{r.get('type', '')}] {source_label}")
            lines.append(r.get("content", ""))
            lines.append("")

        return "\n".join(lines)

    # ── 管理 ──────────────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """CLI 同步查询接口。"""
        return self.retrieve(query, top_k=limit)

    def delete_by_source(self, source: str) -> int:
        """按来源（URL / 文件路径）删除知识条目。返回删除数。"""
        count = 0
        for memory_type in ("web_page", "document", "task_result"):
            try:
                items = self._store.search_memory(
                    query=source,
                    n_results=100,
                    memory_type=memory_type,
                )
                ids_to_delete = []
                for item in items:
                    meta = item.get("metadata", {})
                    item_source = (
                        meta.get("source_url", "")
                        or meta.get("source_path", "")
                    )
                    if source in item_source:
                        ids_to_delete.append(item.get("id", ""))
                for mid in ids_to_delete:
                    if mid and self._store.delete_memory(mid):
                        count += 1
            except Exception:
                pass
        return count

    def get_stats(self) -> Dict[str, Any]:
        """返回知识库统计信息。"""
        return self._store.get_stats()

    # ── 内部 ──────────────────────────────────────────────────────────────

    def _split_text(self, text: str) -> List[str]:
        """将长文本按固定窗口分块。"""
        if not text:
            return []
        text = text.strip()
        if len(text) < 100:
            return [text] if text else []

        chunks: List[str] = []
        start = 0
        while start < len(text):
            end = start + self.CHUNK_SIZE
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            start = end - self.CHUNK_OVERLAP

        return chunks
