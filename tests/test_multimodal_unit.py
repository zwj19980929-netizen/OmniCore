"""
Tests for utils/multimodal_input.py and utils/document_parser.py
"""

import os
import tempfile

import pytest

from utils.document_parser import extract_text
from utils.multimodal_input import (
    IMAGE_EXTENSIONS,
    AUDIO_EXTENSIONS,
    DOCUMENT_EXTENSIONS,
    MultimodalInput,
    MultimodalInputProcessor,
    build_multimodal_input,
    classify_file,
)


# ---------------------------------------------------------------------------
# classify_file
# ---------------------------------------------------------------------------

class TestClassifyFile:
    def test_image_extensions(self):
        for ext in IMAGE_EXTENSIONS:
            assert classify_file(f"/tmp/photo{ext}") == "image"

    def test_audio_extensions(self):
        for ext in AUDIO_EXTENSIONS:
            assert classify_file(f"/tmp/recording{ext}") == "audio"

    def test_document_extensions(self):
        for ext in DOCUMENT_EXTENSIONS:
            assert classify_file(f"/tmp/file{ext}") == "document"

    def test_unknown_extension(self):
        assert classify_file("/tmp/archive.zip") is None
        assert classify_file("/tmp/binary.exe") is None

    def test_case_insensitive(self):
        assert classify_file("/tmp/PHOTO.PNG") == "image"
        assert classify_file("/tmp/DOC.PDF") == "document"


# ---------------------------------------------------------------------------
# build_multimodal_input
# ---------------------------------------------------------------------------

class TestBuildMultimodalInput:
    def test_image(self):
        inp = build_multimodal_input("/tmp/test.png", "描述一下")
        assert inp is not None
        assert inp.image_paths == ["/tmp/test.png"]
        assert inp.text == "描述一下"

    def test_audio(self):
        inp = build_multimodal_input("/tmp/voice.mp3")
        assert inp is not None
        assert inp.audio_path == "/tmp/voice.mp3"

    def test_document(self):
        inp = build_multimodal_input("/tmp/report.pdf", "总结")
        assert inp is not None
        assert inp.document_paths == ["/tmp/report.pdf"]

    def test_unsupported(self):
        assert build_multimodal_input("/tmp/archive.zip") is None


# ---------------------------------------------------------------------------
# document_parser.extract_text
# ---------------------------------------------------------------------------

class TestExtractText:
    def test_plain_text(self, tmp_path):
        f = tmp_path / "note.txt"
        f.write_text("Hello, world!", encoding="utf-8")
        assert extract_text(str(f)) == "Hello, world!"

    def test_markdown(self, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("# Title\n\nBody text.", encoding="utf-8")
        result = extract_text(str(f))
        assert "Title" in result
        assert "Body text" in result

    def test_csv(self, tmp_path):
        f = tmp_path / "data.csv"
        f.write_text("a,b,c\n1,2,3", encoding="utf-8")
        assert "a,b,c" in extract_text(str(f))

    def test_nonexistent_file(self):
        assert extract_text("/tmp/this_file_does_not_exist_xyz.txt") == ""

    def test_unsupported_format(self, tmp_path):
        f = tmp_path / "archive.zip"
        f.write_bytes(b"PK\x03\x04")
        assert extract_text(str(f)) == ""

    def test_gbk_fallback(self, tmp_path):
        f = tmp_path / "gbk.txt"
        f.write_bytes("中文内容".encode("gbk"))
        result = extract_text(str(f))
        assert "中文内容" in result


# ---------------------------------------------------------------------------
# MultimodalInputProcessor
# ---------------------------------------------------------------------------

class TestMultimodalInputProcessor:
    def test_text_only(self):
        processor = MultimodalInputProcessor()
        inp = MultimodalInput(text="查一下天气")
        result = processor.process(inp)
        assert result == "查一下天气"

    def test_empty_input(self):
        processor = MultimodalInputProcessor()
        inp = MultimodalInput()
        result = processor.process(inp)
        assert result == ""

    def test_document_processing(self, tmp_path):
        doc = tmp_path / "test.txt"
        doc.write_text("这是一份测试文档的内容。", encoding="utf-8")

        processor = MultimodalInputProcessor()
        inp = MultimodalInput(
            text="帮我总结",
            document_paths=[str(doc)],
        )
        result = processor.process(inp)
        assert "帮我总结" in result
        assert "测试文档" in result
        assert "[文档内容]" in result

    def test_document_nonexistent_graceful(self, tmp_path):
        processor = MultimodalInputProcessor()
        inp = MultimodalInput(
            text="总结",
            document_paths=["/tmp/nonexistent_abc.pdf"],
        )
        result = processor.process(inp)
        # 文档不存在时仍保留文字部分
        assert "总结" in result

    def test_multiple_documents(self, tmp_path):
        files = []
        for i in range(4):
            f = tmp_path / f"doc{i}.txt"
            f.write_text(f"文档{i}内容", encoding="utf-8")
            files.append(str(f))

        processor = MultimodalInputProcessor()
        inp = MultimodalInput(document_paths=files)
        result = processor.process(inp)
        # 最多处理 3 个
        assert "文档0" in result
        assert "文档2" in result
        assert "文档3" not in result

    def test_audio_disabled_by_default(self):
        """MULTIMODAL_AUDIO_ENABLED defaults to false, so audio should be skipped."""
        processor = MultimodalInputProcessor()
        inp = MultimodalInput(text="附加文字", audio_path="/tmp/fake.mp3")
        result = processor.process(inp)
        # 语音默认关闭，不应报错，只返回文字
        assert "附加文字" in result
