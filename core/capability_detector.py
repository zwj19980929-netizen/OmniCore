"""
OmniCore 任务能力检测器
- 从用户指令中识别所需的模型能力
- 支持关键词匹配 + 参数检测 + 文件扩展名检测
"""
from typing import Set, Dict, Any, List, Optional
from core.model_registry import ModelCapability


class CapabilityDetector:
    """从用户指令中检测所需的模型能力"""

    # 关键词映射（中英文）
    CAPABILITY_KEYWORDS: Dict[ModelCapability, List[str]] = {
        ModelCapability.VISION: [
            "图片", "图像", "截图", "看图", "识别图", "ocr", "照片", "图中",
            "image", "photo", "screenshot", "picture", "看这张", "分析图",
            "验证码", "captcha",
        ],
        ModelCapability.IMAGE_GEN: [
            "生成图片", "画", "绘制", "生成一张", "画一个", "创建图片",
            "generate image", "draw", "create image", "make a picture",
        ],
        ModelCapability.SPEECH_TO_TEXT: [
            "语音识别", "听", "转文字", "音频转", "语音转",
            "transcribe", "speech to text", "audio to text",
        ],
        ModelCapability.TEXT_TO_SPEECH: [
            "朗读", "语音合成", "读出来", "转语音", "生成语音",
            "speak", "tts", "text to speech", "read aloud",
        ],
        ModelCapability.TEXT_LONG: [
            "长文", "文档", "pdf", "分析文件", "整个文件", "全文",
            "long document", "entire file", "full text",
        ],
        ModelCapability.CODE: [
            "代码", "编程", "函数", "脚本", "程序", "debug", "重构",
            "code", "programming", "function", "script", "refactor",
        ],
        ModelCapability.REASONING: [
            "推理", "逻辑", "数学", "证明", "分析问题", "复杂问题",
            "reasoning", "logic", "math", "prove", "complex problem",
        ],
    }

    # 文件扩展名到能力的映射
    FILE_EXT_CAPABILITIES: Dict[str, ModelCapability] = {
        ".png": ModelCapability.VISION,
        ".jpg": ModelCapability.VISION,
        ".jpeg": ModelCapability.VISION,
        ".gif": ModelCapability.VISION,
        ".webp": ModelCapability.VISION,
        ".bmp": ModelCapability.VISION,
        ".svg": ModelCapability.VISION,
        ".mp3": ModelCapability.SPEECH_TO_TEXT,
        ".wav": ModelCapability.SPEECH_TO_TEXT,
        ".m4a": ModelCapability.SPEECH_TO_TEXT,
        ".ogg": ModelCapability.SPEECH_TO_TEXT,
        ".flac": ModelCapability.SPEECH_TO_TEXT,
        ".pdf": ModelCapability.TEXT_LONG,
    }

    def detect(
        self,
        user_input: str,
        task_params: Optional[Dict[str, Any]] = None,
        file_paths: Optional[List[str]] = None,
    ) -> Set[ModelCapability]:
        """
        检测用户指令需要的能力集合

        Args:
            user_input: 用户原始输入
            task_params: 任务参数（可能包含 image_path, audio_path 等）
            file_paths: 涉及的文件路径列表

        Returns:
            所需能力的集合
        """
        capabilities: Set[ModelCapability] = {ModelCapability.TEXT_CHAT}
        input_lower = user_input.lower()

        # 1. 关键词匹配
        for cap, keywords in self.CAPABILITY_KEYWORDS.items():
            if any(kw in input_lower for kw in keywords):
                capabilities.add(cap)

        # 2. 参数检测
        if task_params:
            if task_params.get("image_path") or task_params.get("image_url"):
                capabilities.add(ModelCapability.VISION)
            if task_params.get("audio_path") or task_params.get("audio_url"):
                capabilities.add(ModelCapability.SPEECH_TO_TEXT)
            if task_params.get("generate_image"):
                capabilities.add(ModelCapability.IMAGE_GEN)
            if task_params.get("generate_speech"):
                capabilities.add(ModelCapability.TEXT_TO_SPEECH)

        # 3. 文件扩展名检测
        if file_paths:
            for fp in file_paths:
                ext = ("." + fp.rsplit(".", 1)[-1].lower()) if "." in fp else ""
                if ext in self.FILE_EXT_CAPABILITIES:
                    capabilities.add(self.FILE_EXT_CAPABILITIES[ext])

        # 4. 长文本检测
        if len(user_input) > 10000:
            capabilities.add(ModelCapability.TEXT_LONG)

        return capabilities

    def get_primary_capability(
        self, capabilities: Set[ModelCapability]
    ) -> ModelCapability:
        """
        从能力集合中选出主要能力（用于模型选择）

        优先级：IMAGE_GEN > VISION > STT > TTS > REASONING > CODE > TEXT_LONG > TEXT_CHAT
        """
        priority = [
            ModelCapability.IMAGE_GEN,
            ModelCapability.VISION,
            ModelCapability.SPEECH_TO_TEXT,
            ModelCapability.TEXT_TO_SPEECH,
            ModelCapability.REASONING,
            ModelCapability.CODE,
            ModelCapability.TEXT_LONG,
            ModelCapability.TEXT_CHAT,
        ]

        for cap in priority:
            if cap in capabilities:
                return cap

        return ModelCapability.TEXT_CHAT
