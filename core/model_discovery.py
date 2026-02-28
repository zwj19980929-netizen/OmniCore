"""
OmniCore 动态模型发现模块
- 调用各厂家 API 获取实时可用模型列表
- 缓存结果避免频繁请求
- 发现接口、鉴权方式由 config/models.yaml 的 provider_config 驱动
"""
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import os
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

    def __init__(self, api_keys: Dict[str, str], provider_config: Optional[Dict[str, Any]] = None):
        self.api_keys = api_keys
        self.provider_config = provider_config or {}
        self._cache: Dict[str, List[ModelInfo]] = {}
        self._cache_time: Dict[str, datetime] = {}

    def _is_cache_valid(self, provider: str) -> bool:
        if provider not in self._cache_time:
            return False
        return datetime.now() - self._cache_time[provider] < self.CACHE_TTL

    def _set_cache(self, provider: str, models: List[ModelInfo]):
        self._cache[provider] = models
        self._cache_time[provider] = datetime.now()

    def _get_provider_cfg(self, provider: str) -> Dict[str, Any]:
        cfg = self.provider_config.get(provider, {})
        return cfg if isinstance(cfg, dict) else {}

    def _get_api_key(self, provider: str) -> str:
        key = self.api_keys.get(provider, "")
        if key:
            return key

        cfg = self._get_provider_cfg(provider)
        env_name = cfg.get("api_key_env", "")
        if isinstance(env_name, str) and env_name and env_name.isupper() and "_" in env_name:
            return os.getenv(env_name, "")
        return ""

    def _get_list_endpoint(self, provider: str, default_endpoint: str) -> str:
        cfg = self._get_provider_cfg(provider)
        endpoint = cfg.get("list_endpoint")
        if endpoint:
            return endpoint

        api_base = cfg.get("api_base")
        if api_base:
            return f"{str(api_base).rstrip('/')}/models"

        return default_endpoint

    def _build_auth(self, provider: str, api_key: str) -> Tuple[Dict[str, str], Dict[str, str]]:
        cfg = self._get_provider_cfg(provider)
        auth_mode = str(cfg.get("auth_mode", "bearer")).lower()

        headers: Dict[str, str] = {}
        params: Dict[str, str] = {}

        if not api_key:
            return headers, params

        if auth_mode == "query_key":
            params["key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"

        return headers, params

    def _request_models(self, provider: str, default_endpoint: str) -> Optional[Dict[str, Any]]:
        api_key = self._get_api_key(provider)
        if not api_key:
            logger.warning(f"未配置 {provider.upper()} API key，跳过 {provider} 模型发现")
            return None

        endpoint = self._get_list_endpoint(provider, default_endpoint)
        headers, params = self._build_auth(provider, api_key)

        try:
            resp = requests.get(endpoint, headers=headers, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"{provider} 模型发现失败: {e}")
            return None

    def _iter_model_records(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        if isinstance(data.get("models"), list):
            return data["models"]
        if isinstance(data.get("data"), list):
            return data["data"]
        return []

    def list_gemini_models(self) -> List[ModelInfo]:
        """
        Gemini/代理兼容模式：
        - 官方返回: {"models": [...]}，字段含 name/displayName/inputTokenLimit
        - 代理可能返回: {"data": [...]}，字段含 id/name
        """
        if self._is_cache_valid("gemini"):
            return self._cache["gemini"]

        data = self._request_models(
            provider="gemini",
            default_endpoint="https://generativelanguage.googleapis.com/v1beta/models",
        )
        if data is None:
            return self._cache.get("gemini", [])

        models: List[ModelInfo] = []
        for m in self._iter_model_records(data):
            model_id = (
                m.get("id")
                or m.get("name", "").replace("models/", "")
            )
            if not model_id:
                continue

            models.append(ModelInfo(
                id=model_id,
                provider="gemini",
                display_name=m.get("displayName") or m.get("display_name") or m.get("id"),
                input_token_limit=m.get("inputTokenLimit") or m.get("input_token_limit"),
                output_token_limit=m.get("outputTokenLimit") or m.get("output_token_limit"),
                supported_methods=m.get("supportedGenerationMethods") or m.get("supported_generation_methods") or [],
            ))

        self._set_cache("gemini", models)
        logger.info(f"Gemini 模型发现完成，共 {len(models)} 个模型")
        return models

    def list_kimi_models(self) -> List[ModelInfo]:
        """
        Kimi/Moonshot - OpenAI 兼容格式
        """
        if self._is_cache_valid("kimi"):
            return self._cache["kimi"]

        data = self._request_models(
            provider="kimi",
            default_endpoint="https://api.moonshot.cn/v1/models",
        )
        if data is None:
            return self._cache.get("kimi", [])

        models: List[ModelInfo] = []
        for m in self._iter_model_records(data):
            model_id = m.get("id") or m.get("name")
            if not model_id:
                continue
            models.append(ModelInfo(
                id=model_id,
                provider="kimi",
                display_name=model_id,
            ))

        self._set_cache("kimi", models)
        logger.info(f"Kimi 模型发现完成，共 {len(models)} 个模型")
        return models

    def list_openai_models(self) -> List[ModelInfo]:
        """
        OpenAI - GET /models
        只返回 id/owned_by，token 限制需要静态配置补充
        """
        if self._is_cache_valid("openai"):
            return self._cache["openai"]

        data = self._request_models(
            provider="openai",
            default_endpoint="https://api.openai.com/v1/models",
        )
        if data is None:
            return self._cache.get("openai", [])

        models: List[ModelInfo] = []
        for m in self._iter_model_records(data):
            model_id = m.get("id") or m.get("name")
            if not model_id:
                continue
            models.append(ModelInfo(
                id=model_id,
                provider="openai",
                display_name=model_id,
            ))

        self._set_cache("openai", models)
        logger.info(f"OpenAI 模型发现完成，共 {len(models)} 个模型")
        return models

    def list_deepseek_models(self) -> List[ModelInfo]:
        """
        DeepSeek - GET /models
        OpenAI 兼容格式
        """
        if self._is_cache_valid("deepseek"):
            return self._cache["deepseek"]

        data = self._request_models(
            provider="deepseek",
            default_endpoint="https://api.deepseek.com/models",
        )
        if data is None:
            return self._cache.get("deepseek", [])

        models: List[ModelInfo] = []
        for m in self._iter_model_records(data):
            model_id = m.get("id") or m.get("name")
            if not model_id:
                continue
            models.append(ModelInfo(
                id=model_id,
                provider="deepseek",
                display_name=model_id,
            ))

        self._set_cache("deepseek", models)
        logger.info(f"DeepSeek 模型发现完成，共 {len(models)} 个模型")
        return models

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
