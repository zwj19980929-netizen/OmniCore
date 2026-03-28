"""
MultimodalInputProcessor — 将多模态输入统一转换为文本 user_input。

处理顺序：
1. 语音文件 → Whisper → 文本
2. 图片 → Vision LLM → 意图描述
3. 文档（PDF/DOCX/TXT）→ 文本提取 + 可选摘要
4. 组合 → 合并为最终 user_input
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from config.settings import settings
from utils.logger import log_agent_action, log_warning


# 按类型分组的支持扩展名
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".webm")
DOCUMENT_EXTENSIONS = (".pdf", ".docx", ".txt", ".md", ".markdown", ".csv")


@dataclass
class MultimodalInput:
    """多模态输入的结构化表示。"""
    text: str = ""
    image_paths: List[str] = field(default_factory=list)
    audio_path: str = ""
    document_paths: List[str] = field(default_factory=list)


def classify_file(file_path: str) -> Optional[str]:
    """根据扩展名判断文件类型，返回 'image' / 'audio' / 'document' / None。"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in DOCUMENT_EXTENSIONS:
        return "document"
    return None


def build_multimodal_input(file_path: str, text_hint: str = "") -> Optional[MultimodalInput]:
    """根据文件路径自动构建 MultimodalInput，不支持的格式返回 None。"""
    kind = classify_file(file_path)
    if kind == "image":
        return MultimodalInput(text=text_hint, image_paths=[file_path])
    if kind == "audio":
        return MultimodalInput(text=text_hint, audio_path=file_path)
    if kind == "document":
        return MultimodalInput(text=text_hint, document_paths=[file_path])
    return None


class MultimodalInputProcessor:
    """将多模态输入转换为标准文本 user_input。"""

    def __init__(self):
        self._vision_llm = None
        self._asr_llm = None

    def _get_vision_llm(self):
        if self._vision_llm is None:
            from core.llm import LLMClient
            self._vision_llm = LLMClient.for_vision()
        return self._vision_llm

    def _get_asr_llm(self):
        if self._asr_llm is None:
            from core.llm import LLMClient
            self._asr_llm = LLMClient()
        return self._asr_llm

    def process(self, inp: MultimodalInput) -> str:
        """
        将多模态输入处理为最终 user_input 字符串。

        处理策略：
        - 只有语音：转文字后作为 user_input
        - 只有图片 + 文字：用 Vision 描述图片，追加到文字后
        - 只有图片（无文字）：Vision 推断意图
        - 文档：提取文本摘要，追加为上下文
        - 混合：逐步处理后合并
        """
        parts = []

        # 1. 处理语音输入
        if inp.audio_path and settings.MULTIMODAL_AUDIO_ENABLED:
            transcript = self._process_audio(inp.audio_path)
            if transcript:
                parts.append(transcript)

        # 2. 处理文字部分
        if inp.text:
            parts.append(inp.text)

        # 3. 处理图片
        if inp.image_paths and settings.MULTIMODAL_IMAGE_ENABLED:
            image_context = self._process_images(
                inp.image_paths,
                hint=" ".join(parts) if parts else "",
            )
            if image_context:
                parts.append(f"\n[图片内容]\n{image_context}")

        # 4. 处理文档
        if inp.document_paths and settings.MULTIMODAL_DOCUMENT_ENABLED:
            doc_context = self._process_documents(inp.document_paths)
            if doc_context:
                parts.append(f"\n[文档内容]\n{doc_context}")

        return "\n".join(p for p in parts if p).strip()

    def _process_audio(self, audio_path: str) -> str:
        """语音转文字（Whisper）。"""
        try:
            llm = self._get_asr_llm()
            transcript = llm.transcribe(audio_path, language="zh")
            log_agent_action("MultimodalInput", f"语音已转录: {len(transcript)} 字符")
            return transcript
        except Exception as e:
            log_warning(f"语音转录失败: {e}")
            return ""

    def _process_images(self, image_paths: List[str], hint: str = "") -> str:
        """图片 → Vision LLM → 描述/意图。"""
        descriptions = []
        llm = self._get_vision_llm()

        prompt = (
            f"用户意图提示：{hint}\n\n" if hint else ""
        ) + (
            "请描述图片内容，并推断用户可能想用这张图片完成什么任务。"
            "如果图片包含文字，请完整提取。如果是截图，描述界面内容。"
            "回答简洁，100字以内。"
        )

        for path in image_paths[:3]:  # 最多处理 3 张
            try:
                response = llm.chat_with_image(text=prompt, image=path)
                descriptions.append(response.content)
                log_agent_action("MultimodalInput", f"图片已处理: {Path(path).name}")
            except Exception as e:
                log_warning(f"图片处理失败 ({Path(path).name}): {e}")

        return "\n".join(descriptions)

    def _process_documents(self, doc_paths: List[str]) -> str:
        """文档提取 + 可选摘要。"""
        from utils.document_parser import extract_text

        summaries = []

        for path in doc_paths[:3]:  # 最多 3 个文档
            try:
                content = extract_text(path)
                if not content:
                    continue

                # 长文档截断后用 LLM 摘要
                if len(content) > 8000:
                    try:
                        from core.llm import LLMClient
                        llm = LLMClient()
                        prompt = f"请用200字以内摘要以下文档内容：\n\n{content[:8000]}"
                        response = llm.chat_with_system("你是文档摘要助手。", prompt)
                        summary = response.content
                    except Exception:
                        summary = content[:2000] + "\n...(文档过长，已截断)"
                else:
                    summary = content[:2000]

                filename = Path(path).name
                summaries.append(f"文件：{filename}\n{summary}")
                log_agent_action("MultimodalInput", f"文档已处理: {filename}")

            except Exception as e:
                log_warning(f"文档处理失败 ({Path(path).name}): {e}")

        return "\n\n".join(summaries)
