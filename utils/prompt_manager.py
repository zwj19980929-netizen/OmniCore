"""
OmniCore Prompt 管理器
统一管理所有 LLM 提示词，支持热更新和版本管理
"""
from pathlib import Path
from typing import Dict, Optional
import yaml

from utils.logger import log_warning, logger


class PromptManager:
    """
    提示词管理器
    从 prompts/ 目录加载和管理所有提示词
    """

    def __init__(self, prompts_dir: str = None):
        self.prompts_dir = Path(prompts_dir or Path(__file__).parent.parent / "prompts")
        self._prompts_cache: Dict[str, str] = {}
        self._load_all_prompts()

    def _load_all_prompts(self):
        """加载所有提示词文件"""
        if not self.prompts_dir.exists():
            log_warning(f"提示词目录不存在: {self.prompts_dir}")
            return

        # 加载 .txt 文件
        for txt_file in self.prompts_dir.glob("*.txt"):
            prompt_name = txt_file.stem
            try:
                content = txt_file.read_text(encoding="utf-8")
                self._prompts_cache[prompt_name] = content
                logger.debug(f"加载提示词: {prompt_name}")
            except Exception as e:
                log_warning(f"加载提示词失败 [{prompt_name}]: {e}")

        # 加载 .yaml 文件（支持多个 prompt 在一个文件中）
        for yaml_file in self.prompts_dir.glob("*.yaml"):
            try:
                with open(yaml_file, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                    if isinstance(data, dict):
                        for key, value in data.items():
                            if isinstance(value, str):
                                self._prompts_cache[key] = value
                                logger.debug(f"加载提示词: {key} (from {yaml_file.name})")
            except Exception as e:
                log_warning(f"加载 YAML 提示词失败 [{yaml_file.name}]: {e}")

    def get(self, prompt_name: str, default: str = "") -> str:
        """
        获取提示词

        Args:
            prompt_name: 提示词名称
            default: 默认值（如果未找到）

        Returns:
            提示词内容
        """
        return self._prompts_cache.get(prompt_name, default)

    def reload(self):
        """重新加载所有提示词（热更新）"""
        self._prompts_cache.clear()
        self._load_all_prompts()
        logger.info("提示词已重新加载")

    def list_prompts(self) -> list:
        """列出所有已加载的提示词名称"""
        return list(self._prompts_cache.keys())

    def format(self, prompt_name: str, **kwargs) -> str:
        """
        获取并格式化提示词

        Args:
            prompt_name: 提示词名称
            **kwargs: 格式化参数

        Returns:
            格式化后的提示词
        """
        template = self.get(prompt_name)
        if not template:
            log_warning(f"提示词不存在: {prompt_name}")
            return ""

        try:
            return template.format(**kwargs)
        except KeyError as e:
            log_warning(f"提示词格式化失败 [{prompt_name}]: 缺少参数 {e}")
            return template


# 全局单例
_prompt_manager: Optional[PromptManager] = None


def get_prompt_manager() -> PromptManager:
    """获取全局 PromptManager 单例"""
    global _prompt_manager
    if _prompt_manager is None:
        _prompt_manager = PromptManager()
    return _prompt_manager


def get_prompt(prompt_name: str, default: str = "") -> str:
    """快捷方法：获取提示词"""
    return get_prompt_manager().get(prompt_name, default)


def format_prompt(prompt_name: str, **kwargs) -> str:
    """快捷方法：获取并格式化提示词"""
    return get_prompt_manager().format(prompt_name, **kwargs)
