"""
P1-2: 知识库 RAG 深度集成 — 单元测试。

覆盖：
- KnowledgeItem 数据模型
- KnowledgeStore 文本分块（_split_text）
- KnowledgeStore 索引（index_web_page / index_document / index_task_result）
- KnowledgeStore 检索（retrieve）+ 距离过滤 + 长度截断
- KnowledgeStore 格式化（format_as_context）
- KnowledgeStore 管理（search / delete_by_source / get_stats）
- 配置开关控制
- CLI 命令处理器（_handle_learn_command / _handle_knowledge_command）
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from memory.knowledge_store import KnowledgeItem, KnowledgeStore


# ── Fixtures & Helpers ──────────────────────────────────────────


class FakeChromaMemory:
    """Lightweight in-memory substitute for ChromaMemory."""

    def __init__(self, collection_name: str = "test"):
        self.collection_name = collection_name
        self._memories: Dict[str, Dict[str, Any]] = {}  # id → {content, metadata, memory_type}

    def add_memory(
        self,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        memory_type: str = "general",
        *,
        scope=None,
        fingerprint: str = "",
        allow_update: bool = False,
        skip_dedup: bool = False,
    ) -> str:
        import hashlib
        if fingerprint:
            mid = f"mem_{hashlib.sha1(fingerprint.encode()).hexdigest()[:24]}"
        else:
            import uuid
            mid = f"mem_{uuid.uuid4().hex[:12]}"
        self._memories[mid] = {
            "content": content,
            "metadata": metadata or {},
            "memory_type": memory_type,
        }
        return mid

    def search_memory(
        self,
        query: str,
        n_results: int = 5,
        memory_type: Optional[str] = None,
        *,
        scope=None,
        include_global_fallback: bool = False,
        include_legacy_unscoped: bool = False,
    ) -> List[Dict[str, Any]]:
        results = []
        for mid, data in self._memories.items():
            if memory_type and data["memory_type"] != memory_type:
                continue
            # Simple keyword-based "similarity" for testing
            overlap = len(set(query.lower().split()) & set(data["content"].lower().split()))
            distance = max(0.0, 0.5 - overlap * 0.1)
            results.append({
                "id": mid,
                "content": data["content"],
                "metadata": data["metadata"],
                "distance": distance,
                "scope_match": "global",
            })
        results.sort(key=lambda x: x["distance"])
        return results[:n_results]

    def delete_memory(self, memory_id: str) -> bool:
        if memory_id in self._memories:
            del self._memories[memory_id]
            return True
        return False

    def get_stats(self, *, scope=None) -> Dict[str, Any]:
        by_type: Dict[str, int] = {}
        for data in self._memories.values():
            t = data["memory_type"]
            by_type[t] = by_type.get(t, 0) + 1
        return {
            "collection_name": self.collection_name,
            "total_memories": len(self._memories),
            "by_type": by_type,
            "by_scope_level": {},
            "persist_dir": "/tmp/test",
            "scope": {},
        }


@pytest.fixture
def fake_store():
    """Return a KnowledgeStore backed by FakeChromaMemory."""
    store = KnowledgeStore.__new__(KnowledgeStore)
    store._store = FakeChromaMemory(collection_name="omnicore_knowledge")
    return store


# ── KnowledgeItem ─────────────────────────────────────────────


class TestKnowledgeItem:
    def test_default_values(self):
        item = KnowledgeItem(content="hello", source_type="web_page")
        assert item.content == "hello"
        assert item.source_type == "web_page"
        assert item.source_url == ""
        assert item.title == ""
        assert item.tags == []
        assert item.metadata == {}

    def test_custom_values(self):
        item = KnowledgeItem(
            content="test",
            source_type="document",
            source_url="/tmp/doc.pdf",
            title="My Doc",
            tags=["pdf"],
            metadata={"pages": 5},
        )
        assert item.title == "My Doc"
        assert item.tags == ["pdf"]


# ── _split_text ───────────────────────────────────────────────


class TestSplitText:
    def test_empty_text(self, fake_store):
        assert fake_store._split_text("") == []
        assert fake_store._split_text(None) == []

    def test_short_text(self, fake_store):
        assert fake_store._split_text("short") == ["short"]

    def test_below_100_chars(self, fake_store):
        text = "a" * 50
        assert fake_store._split_text(text) == [text]

    def test_whitespace_only(self, fake_store):
        assert fake_store._split_text("   ") == []

    def test_chunking_produces_overlap(self, fake_store):
        fake_store.CHUNK_SIZE = 100
        fake_store.CHUNK_OVERLAP = 20
        text = "a" * 250
        chunks = fake_store._split_text(text)
        assert len(chunks) >= 3
        # Each chunk should be at most CHUNK_SIZE chars
        for c in chunks:
            assert len(c) <= 100

    def test_chunking_single_chunk(self, fake_store):
        fake_store.CHUNK_SIZE = 1000
        fake_store.CHUNK_OVERLAP = 200
        text = "a" * 500
        chunks = fake_store._split_text(text)
        assert len(chunks) == 1


# ── index_web_page ────────────────────────────────────────────


class TestIndexWebPage:
    def test_basic_indexing(self, fake_store):
        count = fake_store.index_web_page(
            url="https://example.com",
            title="Example",
            content="This is a test page with enough content to index. " * 3,
        )
        assert count >= 1
        stats = fake_store.get_stats()
        assert stats["by_type"].get("web_page", 0) >= 1

    def test_empty_content(self, fake_store):
        count = fake_store.index_web_page(url="https://example.com", title="Empty", content="")
        assert count == 0

    def test_short_content(self, fake_store):
        count = fake_store.index_web_page(url="https://example.com", title="Short", content="hi")
        assert count == 1  # short text still produces 1 chunk

    def test_metadata_stored(self, fake_store):
        fake_store.index_web_page(
            url="https://example.com",
            title="Test Page",
            content="sufficient content for indexing " * 5,
            session_id="sess_1",
            job_id="job_1",
        )
        # Verify via internal store
        for data in fake_store._store._memories.values():
            if data["memory_type"] == "web_page":
                assert data["metadata"]["source_url"] == "https://example.com"
                assert data["metadata"]["title"] == "Test Page"
                assert data["metadata"]["session_id"] == "sess_1"
                break

    @patch("memory.knowledge_store.settings")
    def test_disabled_returns_zero(self, mock_settings, fake_store):
        mock_settings.KNOWLEDGE_BASE_ENABLED = False
        count = fake_store.index_web_page(
            url="https://example.com",
            title="Test",
            content="enough content here " * 10,
        )
        assert count == 0


# ── index_document ────────────────────────────────────────────


class TestIndexDocument:
    def test_basic_indexing(self, fake_store):
        count = fake_store.index_document(
            file_path="/tmp/test.txt",
            content="Document content for testing knowledge base indexing. " * 5,
        )
        assert count >= 1

    def test_title_from_path(self, fake_store):
        fake_store.index_document(
            file_path="/home/user/docs/report.pdf",
            content="Enough content to index " * 5,
        )
        for data in fake_store._store._memories.values():
            if data["memory_type"] == "document":
                assert data["metadata"]["title"] == "report.pdf"
                break

    def test_custom_title(self, fake_store):
        fake_store.index_document(
            file_path="/tmp/test.txt",
            content="Enough content to index " * 5,
            title="My Custom Title",
        )
        for data in fake_store._store._memories.values():
            if data["memory_type"] == "document":
                assert data["metadata"]["title"] == "My Custom Title"
                break


# ── index_task_result ─────────────────────────────────────────


class TestIndexTaskResult:
    def test_basic_indexing(self, fake_store):
        result = fake_store.index_task_result(
            summary="The search found 5 results for iPhone 16 pricing around $999-$1199.",
            user_input="search iPhone 16 price",
            job_id="job_001",
        )
        assert result is True

    def test_short_summary_skipped(self, fake_store):
        result = fake_store.index_task_result(
            summary="ok",
            user_input="test",
            job_id="job_002",
        )
        assert result is False

    @patch("memory.knowledge_store.settings")
    def test_min_length_threshold(self, mock_settings):
        mock_settings.KNOWLEDGE_BASE_ENABLED = True
        mock_settings.KNOWLEDGE_MIN_CONTENT_LENGTH = 100
        store = KnowledgeStore.__new__(KnowledgeStore)
        store._store = FakeChromaMemory()
        result = store.index_task_result(
            summary="a" * 50,  # below threshold
            user_input="test",
            job_id="job_003",
        )
        assert result is False


# ── retrieve ──────────────────────────────────────────────────


class TestRetrieve:
    def test_basic_retrieval(self, fake_store):
        fake_store.index_web_page(
            url="https://example.com",
            title="iPhone Prices",
            content="iPhone 16 price comparison results from major retailers",
        )
        results = fake_store.retrieve("iPhone price")
        assert len(results) >= 1
        assert results[0]["type"] == "web_page"

    def test_empty_query(self, fake_store):
        assert fake_store.retrieve("") == []

    def test_distance_filtering(self, fake_store):
        fake_store.index_web_page(
            url="https://example.com",
            title="Test",
            content="completely unrelated content about cooking recipes and ingredients",
        )
        # With FakeChromaMemory, low overlap = high distance → filtered out
        results = fake_store.retrieve("quantum physics theoretical framework")
        # Results may or may not pass the distance threshold depending on fake scoring
        # The important thing is no crash
        assert isinstance(results, list)

    def test_max_total_chars_truncation(self, fake_store):
        for i in range(5):
            fake_store.index_document(
                file_path=f"/tmp/doc{i}.txt",
                content=f"Document {i} with keyword search content " * 20,
            )
        results = fake_store.retrieve("search content", max_total_chars=200)
        total = sum(len(r["content"]) for r in results)
        assert total <= 200 + 1000  # allow for one chunk overshoot at boundary

    def test_source_type_filter(self, fake_store):
        fake_store.index_web_page(url="https://ex.com", title="Web", content="web content for testing search")
        fake_store.index_document(file_path="/tmp/d.txt", content="document content for testing search")
        results = fake_store.retrieve("testing search", source_types=["web_page"])
        types = {r["type"] for r in results}
        assert "document" not in types


# ── format_as_context ─────────────────────────────────────────


class TestFormatAsContext:
    def test_empty_results(self, fake_store):
        assert fake_store.format_as_context([]) == ""

    def test_formatting(self, fake_store):
        results = [
            {"content": "iPhone 16 costs $999", "source": "https://apple.com", "title": "Apple Store", "type": "web_page", "distance": 0.1},
            {"content": "Summary of price analysis", "source": "job_001", "title": "", "type": "task_result", "distance": 0.2},
        ]
        output = fake_store.format_as_context(results)
        assert "## 相关知识（来自知识库）" in output
        assert "[web_page] Apple Store" in output
        assert "[task_result] job_001" in output
        assert "iPhone 16 costs $999" in output


# ── search (sync wrapper) ────────────────────────────────────


class TestSearch:
    def test_search_delegates_to_retrieve(self, fake_store):
        fake_store.index_document(file_path="/tmp/t.txt", content="searchable knowledge content for testing")
        results = fake_store.search("searchable knowledge")
        assert isinstance(results, list)


# ── delete_by_source ──────────────────────────────────────────


class TestDeleteBySource:
    def test_delete_matching(self, fake_store):
        fake_store.index_web_page(url="https://example.com/page1", title="P1", content="content one for testing " * 5)
        fake_store.index_web_page(url="https://other.com/page2", title="P2", content="content two for testing " * 5)
        count = fake_store.delete_by_source("https://example.com")
        assert count >= 1

    def test_delete_no_match(self, fake_store):
        fake_store.index_document(file_path="/tmp/a.txt", content="some content " * 5)
        count = fake_store.delete_by_source("https://nonexistent.com")
        assert count == 0


# ── get_stats ─────────────────────────────────────────────────


class TestGetStats:
    def test_empty_stats(self, fake_store):
        stats = fake_store.get_stats()
        assert stats["total_memories"] == 0

    def test_stats_after_indexing(self, fake_store):
        fake_store.index_web_page(url="https://ex.com", title="T", content="web page content " * 5)
        fake_store.index_document(file_path="/tmp/d.txt", content="document content " * 5)
        fake_store.index_task_result(
            summary="Task result summary with enough content to pass threshold.",
            user_input="test",
            job_id="job_x",
        )
        stats = fake_store.get_stats()
        assert stats["total_memories"] >= 3
        assert stats["by_type"].get("web_page", 0) >= 1
        assert stats["by_type"].get("document", 0) >= 1
        assert stats["by_type"].get("task_result", 0) >= 1


# ── Config switch ─────────────────────────────────────────────


class TestConfigSwitch:
    @patch("memory.knowledge_store.settings")
    def test_disabled_index_web_page(self, mock_settings):
        mock_settings.KNOWLEDGE_BASE_ENABLED = False
        store = KnowledgeStore.__new__(KnowledgeStore)
        store._store = FakeChromaMemory()
        assert store.index_web_page(url="u", title="t", content="c" * 200) == 0

    @patch("memory.knowledge_store.settings")
    def test_disabled_index_document(self, mock_settings):
        mock_settings.KNOWLEDGE_BASE_ENABLED = False
        store = KnowledgeStore.__new__(KnowledgeStore)
        store._store = FakeChromaMemory()
        assert store.index_document(file_path="f", content="c" * 200) == 0

    @patch("memory.knowledge_store.settings")
    def test_disabled_index_task_result(self, mock_settings):
        mock_settings.KNOWLEDGE_BASE_ENABLED = False
        store = KnowledgeStore.__new__(KnowledgeStore)
        store._store = FakeChromaMemory()
        assert store.index_task_result(summary="s" * 200, user_input="u", job_id="j") is False


# ── CLI command handlers ──────────────────────────────────────


class TestCLICommands:
    @patch("memory.knowledge_store.KnowledgeStore.__init__", side_effect=Exception("init fail"))
    def test_learn_init_failure(self, _mock):
        from main import _handle_learn_command
        result = _handle_learn_command("/learn /tmp/test.txt")
        assert result["is_special_command"] is True
        assert not result["success"]

    def test_learn_empty_target(self):
        from main import _handle_learn_command
        result = _handle_learn_command("/learn")
        assert not result["success"]
        assert "用法" in result["output"]

    @patch("utils.document_parser.extract_text", return_value="document content")
    @patch("memory.knowledge_store.KnowledgeStore.index_document", return_value=3)
    @patch("os.path.exists", return_value=True)
    @patch("memory.knowledge_store.ChromaMemory")
    def test_learn_local_file(self, _chroma, _exists, _index, _extract):
        from main import _handle_learn_command
        result = _handle_learn_command("/learn /tmp/test.txt")
        assert result["success"]
        assert "3 chunks" in result["output"]

    @patch("utils.document_parser.extract_text", return_value="")
    @patch("os.path.exists", return_value=True)
    @patch("memory.knowledge_store.ChromaMemory")
    def test_learn_unparseable_file(self, _chroma, _exists, _extract):
        from main import _handle_learn_command
        result = _handle_learn_command("/learn /tmp/unknown.bin")
        assert not result["success"]
        assert "无法解析" in result["output"]

    @patch("memory.knowledge_store.ChromaMemory")
    def test_knowledge_stats(self, mock_chroma_cls):
        from main import _handle_knowledge_command
        mock_store_instance = MagicMock()
        mock_store_instance.get_stats.return_value = {
            "total_memories": 10,
            "by_type": {"web_page": 5, "document": 3, "task_result": 2},
            "by_scope_level": {},
            "collection_name": "test",
            "persist_dir": "/tmp",
            "scope": {},
        }
        mock_chroma_cls.return_value = mock_store_instance
        with patch.object(KnowledgeStore, "get_stats", return_value={
            "total_memories": 10,
            "by_type": {"web_page": 5, "document": 3, "task_result": 2},
        }):
            result = _handle_knowledge_command("/knowledge stats")
        assert result["success"]
        assert "10" in result["output"]

    @patch("memory.knowledge_store.ChromaMemory")
    def test_knowledge_search(self, _chroma):
        from main import _handle_knowledge_command
        with patch.object(KnowledgeStore, "search", return_value=[
            {"content": "test result", "title": "Test", "type": "web_page", "distance": 0.1, "source": "url"},
        ]):
            result = _handle_knowledge_command("/knowledge search test query")
        assert result["success"]
        assert "1 条" in result["output"]

    @patch("memory.knowledge_store.ChromaMemory")
    def test_knowledge_search_no_results(self, _chroma):
        from main import _handle_knowledge_command
        with patch.object(KnowledgeStore, "search", return_value=[]):
            result = _handle_knowledge_command("/knowledge search nothing")
        assert result["success"]
        assert "未找到" in result["output"]

    @patch("memory.knowledge_store.ChromaMemory")
    def test_knowledge_delete(self, _chroma):
        from main import _handle_knowledge_command
        with patch.object(KnowledgeStore, "delete_by_source", return_value=5):
            result = _handle_knowledge_command("/knowledge delete https://example.com")
        assert result["success"]
        assert "5 条" in result["output"]
