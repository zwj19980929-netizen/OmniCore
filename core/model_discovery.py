"""
OmniCore 动态模型发现模块
- 调用各厂家 API 获取实时可用模型列表
- 缓存结果避免频繁请求
- Gemini 返回完整信息（token限制+能力），其他厂家返回基础信息
"""
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import requests

from utils.logger import logger


@dataclass
class ModelInfo:
    """模型信息"""
    id: str
    provider: str
    display_name: Optional[str] = None
    input_token_limit: Optional[int] = None
    output_token_limit: Optional[int] = None
    capabilities: List[str] = field(default_factory=lambda: ["text_chat"])
    supported_methods: List[str] = field(default_factory=list)
    cost_tier: str = "medium"


class ModelDiscovery:
    """
    统一模型发现服务
    - 各厂家 API 动态查询可用模型
    - 1 小时缓存避免频繁请求
    """

    CACHE_TTL = timedelta(hours=1)

    def __init__(self, api_keys: Dict[str, str]):
        self.api_keys = api_keys
        self._cache: Dict[str, List[ModelInfo]] = {}
        self._cache_time: Dict[str, datetime] = {}

    def _is_cache_valid(self, provider: str) -> bool:
        if provider not in self._cache_time:
            return False
        return datetime.now() - self._cache_time[provider] < self.CACHE_TTL

    def _set_cache(self, provider: str, models: List[ModelInfo]):
        self._cache[provider] = models
        self._cache_time[provider] = datetime.now()

    def list_gemini_models(self) -> List[ModelInfo]:
        """
        Gemini - 返回完整信息（token限制 + supportedGenerationMethods）
        GET https://generativelanguage.googleapis.com/v1beta/models?key=KEY
        """
        if self._is_cache_valid("gemini"):
            return self._cache["gemini"]

        key = self.api_keys.get("gemini", "")
        if not key:
            logger.warning("未配置 GEMINI_API_KEY，跳过 Gemini 模型发现")
            return []

        try:
            url = "https://generativelanguage.googleapis.com/v1beta/models"
            resp = requests.get(url, params={"key": key}, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            models = []
            for m in data.get("models", []):
                model_id = m.get("name", "").replace("models/", "")
                if not model_id:
                    continue
                models.append(ModelInfo(
                    id=model_id,
                    provider="gemini",
                    display_name=m.get("displayName"),
                    input_token_limit=m.get("inputTokenLimit"),
                    output_token_limit=m.get("outputTokenLimit"),
                    supported_methods=m.get("supportedGenerationMethods", []),
                ))

            self._set_cache("gemini", models)
            logger.info(f"Gemini 模型发现完成，共 {len(models)} 个模型")
            return models

        except Exception as e:
            logger.error(f"Gemini 模型发现失败: {e}")
            return self._cache.get("gemini", [])

    def list_kimi_models(self) -> List[ModelInfo]:
        """
        Kimi/Moonshot - OpenAI 兼容格式
        GET https://api.moonshot.cn/v1/models
        """
        if self._is_cache_valid("kimi"):
            return self._cache["kimi"]

        key = self.api_keys.get("kimi", "")
        if not key:
            logger.warning("未配置 KIMI_API_KEY，跳过 Kimi 模型发现")
            return []

        try:
            resp = requests.get(
                "https://api.moonshot.cn/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            models = []
            for m in data.get("data", []):
                models.append(ModelInfo(
                    id=m["id"],
                    provider="kimi",
                    display_name=m.get("id"),
                ))

            self._set_cache("kimi", models)
            logger.info(f"Kimi 模型发现完成，共 {len(models)} 个模型")
            return models

        except Exception as e:
            logger.error(f"Kimi 模型发现失败: {e}")
            return self._cache.get("kimi", [])

    def list_openai_models(self) -> List[ModelInfo]:
        """
        OpenAI - GET https://api.openai.com/v1/models
        只返回 id/owned_by，token 限制需要静态配置补充
        """
        if self._is_cache_valid("openai"):
            return self._cache["openai"]

        key = self.api_keys.get("openai", "")
        if not key:
            logger.warning("未配置 OPENAI_API_KEY，跳过 OpenAI 模型发现")
            return []

        try:
            resp = requests.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            models = []
            for m in data.get("data", []):
                models.append(ModelInfo(
                    id=m["id"],
                    provider="openai",
                    display_name=m.get("id"),
                ))

            self._set_cache("openai", models)
            logger.info(f"OpenAI 模型发现完成，共 {len(models)} 个模型")
            return models

        except Exception as e:
            logger.error(f"OpenAI 模型发现失败: {e}")
            return self._cache.get("openai", [])

    def list_deepseek_models(self) -> List[ModelInfo]:
        """
        DeepSeek - GET https://api.deepseek.com/models
        OpenAI 兼容格式
        """
        if self._is_cache_valid("deepseek"):
            return self._cache["deepseek"]

        key = self.api_keys.get("deepseek", "")
        if not key:
            logger.warning("未配置 DEEPSEEK_API_KEY，跳过 DeepSeek 模型发现")
            return []

        try:
            resp = requests.get(
                "https://api.deepseek.com/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            models = []
            for m in data.get("data", []):
                models.append(ModelInfo(
                    id=m["id"],
                    provider="deepseek",
                    display_name=m.get("id"),
                ))

            self._set_cache("deepseek", models)
            logger.info(f"DeepSeek 模型发现完成，共 {len(models)} 个模型")
            return models

        except Exception as e:
            logger.error(f"DeepSeek 模型发现失败: {e}")
            return self._cache.get("deepseek", [])

    def list_models(self, provider: str) -> List[ModelInfo]:
        """按厂家名获取模型列表"""
        method_map = {
            "gemini": self.list_gemini_models,
            "kimi": self.list_kimi_models,
            "openai": self.list_openai_models,
            "deepseek": self.list_deepseek_models,
        }
        fn = method_map.get(provider)
        if not fn:
            logger.error(f"不支持的厂家: {provider}")
            return []
        return fn()

    def list_all(self) -> Dict[str, List[ModelInfo]]:
        """获取所有厂家的模型"""
        result = {}
        for provider in ["gemini", "kimi", "openai", "deepseek"]:
            result[provider] = self.list_models(provider)
        return result

    def refresh_cache(self, provider: str = None):
        """强制刷新缓存"""
        if provider:
            self._cache.pop(provider, None)
            self._cache_time.pop(provider, None)
        else:
            self._cache.clear()
            self._cache_time.clear()

    def is_model_available(self, provider: str, model_id: str) -> bool:
        """检查某个模型是否在厂家的可用列表中"""
        models = self.list_models(provider)
        return any(m.id == model_id for m in models)
