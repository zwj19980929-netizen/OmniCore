"""
OmniCore 模型注册表
- 整合动态发现 + 静态配置 + LiteLLM model_cost
- 根据能力需求智能选择子模型
- 用户锁定厂家后在该厂家内自动选择
"""
from typing import Dict, List, Optional, Set
from enum import Enum
from pathlib import Path
import os
import yaml
import litellm

from core.model_discovery import ModelDiscovery, ModelInfo
from config.settings import settings
from utils.logger import logger


class ModelCapability(Enum):
    """模型能力类型"""
    TEXT_CHAT = "text_chat"
    TEXT_LONG = "text_long"
    VISION = "vision"
    IMAGE_GEN = "image_gen"
    SPEECH_TO_TEXT = "stt"
    TEXT_TO_SPEECH = "tts"
    CODE = "code"
    REASONING = "reasoning"
    EMBEDDING = "embedding"


class ModelRegistry:
    """
    模型注册表
    - 动态查询各厂家可用模型
    - 合并静态能力配置
    - 根据能力需求智能选择模型
    """

    SUPPORTED_PROVIDERS = ["gemini", "kimi", "openai", "deepseek", "minimax", "zhipu"]

    def __init__(self, config_path: str = None):
        self.config_path = Path(config_path or settings.MODELS_CONFIG_PATH)
        self.config = self._load_config()
        self.active_provider: Optional[str] = None
        self.cost_preference: str = getattr(settings, "COST_PREFERENCE", "low")

        # 初始化动态发现服务
        self.discovery = ModelDiscovery(
            self._resolve_api_keys(),
            provider_config=self.config.get("provider_config", {}),
        )

        # 合并后的模型信息缓存 {provider: {model_id: ModelInfo}}
        self._merged_models: Dict[str, Dict[str, ModelInfo]] = {}

        # 启动时设置用户偏好厂家
        preferred = getattr(settings, "PREFERRED_PROVIDER", "")
        if preferred and preferred in self.SUPPORTED_PROVIDERS:
            self.active_provider = preferred

    def _load_config(self) -> dict:
        """加载静态能力配置"""
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        logger.warning(f"模型配置文件不存在: {self.config_path}")
        return {}

    def _resolve_api_keys(self) -> Dict[str, str]:
        """
        解析各 provider 的 API key：
        1) 优先使用 settings 中已加载的常规变量
        2) 若为空，回退到 provider_config.api_key_env 指定的环境变量名
        """
        keys = {
            "gemini": settings.GEMINI_API_KEY,
            "kimi": getattr(settings, "KIMI_API_KEY", ""),
            "openai": settings.OPENAI_API_KEY,
            "deepseek": settings.DEEPSEEK_API_KEY,
            "minimax": getattr(settings, "MINIMAX_API_KEY", ""),
            "zhipu": getattr(settings, "ZHIPU_API_KEY", ""),
        }

        provider_cfg = self.config.get("provider_config", {})
        for provider in self.SUPPORTED_PROVIDERS:
            if keys.get(provider):
                continue
            cfg = provider_cfg.get(provider, {})
            env_name = cfg.get("api_key_env", "")
            if isinstance(env_name, str) and env_name and env_name.isupper() and "_" in env_name:
                keys[provider] = os.getenv(env_name, "")

        return keys

    def _enrich_with_litellm(self, model_id: str, provider: str) -> dict:
        """从 LiteLLM model_cost 补充 token 限制"""
        lookup_keys = [
            f"{provider}/{model_id}",
            model_id,
        ]
        if provider == "gemini":
            lookup_keys.append(f"gemini/{model_id}")

        for key in lookup_keys:
            if key in litellm.model_cost:
                info = litellm.model_cost[key]
                return {
                    "max_input_tokens": info.get("max_input_tokens"),
                    "max_output_tokens": info.get("max_output_tokens"),
                    "max_tokens": info.get("max_tokens"),
                }
        return {}

    def _merge_model_info(self, model: ModelInfo) -> ModelInfo:
        """合并动态查询 + 静态配置 + LiteLLM 信息"""
        provider = model.provider
        model_id = model.id

        # 1. 静态配置
        overrides = self.config.get("capability_overrides", {})
        model_override = overrides.get(provider, {}).get(model_id, {})

        # 2. LiteLLM 补充
        litellm_info = self._enrich_with_litellm(model_id, provider)

        # 3. 合并能力标签
        if model_override.get("capabilities"):
            model.capabilities = model_override["capabilities"]

        # 4. 合并成本等级
        if model_override.get("cost_tier"):
            model.cost_tier = model_override["cost_tier"]

        # 5. 合并 token 限制（优先级：API动态 > 静态配置 > LiteLLM）
        if not model.input_token_limit:
            model.input_token_limit = (
                model_override.get("max_tokens")
                or litellm_info.get("max_input_tokens")
                or litellm_info.get("max_tokens")
            )
        if not model.output_token_limit:
            model.output_token_limit = litellm_info.get("max_output_tokens")

        return model

    def set_provider(self, provider: str):
        """用户锁定厂家"""
        if provider not in self.SUPPORTED_PROVIDERS:
            raise ValueError(f"不支持的厂家: {provider}，可选: {self.SUPPORTED_PROVIDERS}")
        self.active_provider = provider
        logger.info(f"已锁定模型厂家: {provider}")

    def clear_provider(self):
        """清除厂家锁定"""
        self.active_provider = None
        logger.info("已清除厂家锁定，将自动选择最优厂家")

    def refresh(self, provider: str = None):
        """刷新模型列表"""
        self.discovery.refresh_cache(provider)
        if provider:
            self._merged_models.pop(provider, None)
        else:
            self._merged_models.clear()
        logger.info(f"已刷新模型缓存: {provider or '全部'}")

    def get_models(self, provider: str = None) -> Dict[str, ModelInfo]:
        """获取指定厂家的所有模型（已合并能力信息）"""
        target = provider or self.active_provider

        if target:
            if target in self._merged_models:
                return self._merged_models[target]

            raw_models = self.discovery.list_models(target)
            result = {}
            for model in raw_models:
                enriched = self._merge_model_info(model)
                result[model.id] = enriched

            self._merged_models[target] = result
            return result

        # 未指定厂家，获取所有
        all_models = {}
        for p in self.SUPPORTED_PROVIDERS:
            all_models.update(self.get_models(p))
        return all_models

    # 能力 → settings 中用户可配置的环境变量字段名
    _CAPABILITY_ENV_OVERRIDE = {
        ModelCapability.VISION: "VISION_MODEL",
        ModelCapability.TEXT_CHAT: "DEFAULT_MODEL",
    }

    def get_model_for_capability(
        self,
        capability: ModelCapability,
        provider: str = None,
        prefer_cost: str = None,
    ) -> Optional[str]:
        """
        根据能力需求返回最合适的模型

        Returns:
            模型全名 "provider/model_id" 或 None
        """
        # 优先使用用户通过环境变量显式指定的模型
        env_field = self._CAPABILITY_ENV_OVERRIDE.get(capability)
        if env_field and not provider:
            user_model = getattr(settings, env_field, None)
            if user_model and os.getenv(env_field):
                logger.info(f"能力 {capability.value} → 使用用户配置 {env_field}={user_model}")
                return user_model

        target = provider or self.active_provider
        cost_pref = prefer_cost or self.cost_preference

        if target:
            # 在指定厂家内查找
            models = self.get_models(target)
            candidates = [
                (mid, m) for mid, m in models.items()
                if capability.value in (m.capabilities or [])
            ]

            if not candidates:
                logger.warning(f"{target} 没有支持 {capability.value} 的模型")
                return None

            # 按成本排序
            cost_order = {"low": 0, "medium": 1, "high": 2}
            if cost_pref == "high":
                candidates.sort(key=lambda x: -cost_order.get(x[1].cost_tier, 1))
            else:
                candidates.sort(key=lambda x: cost_order.get(x[1].cost_tier, 1))

            best = candidates[0][0]
            logger.info(f"能力 {capability.value} → 选择模型: {target}/{best}")
            return f"{target}/{best}"

        # 未指定厂家，使用全局默认优先级
        defaults = self.config.get("capability_defaults", {})
        default_list = defaults.get(capability.value, [])

        for model_path in default_list:
            if "/" not in model_path:
                continue
            p, mid = model_path.split("/", 1)
            # 验证模型是否真实可用
            models = self.get_models(p)
            if mid in models:
                logger.info(f"能力 {capability.value} → 选择模型: {model_path}")
                return model_path

        logger.warning(f"未找到支持 {capability.value} 的模型")
        return None

    def get_available_capabilities(self, provider: str = None) -> Set[ModelCapability]:
        """获取某厂家支持的所有能力"""
        models = self.get_models(provider)
        caps = set()
        for m in models.values():
            for c in (m.capabilities or []):
                try:
                    caps.add(ModelCapability(c))
                except ValueError:
                    pass
        return caps

    def list_models_with_capability(
        self,
        capability: ModelCapability,
        provider: str = None,
    ) -> List[str]:
        """列出所有支持某能力的模型"""
        target = provider or self.active_provider
        result = []

        providers = [target] if target else self.SUPPORTED_PROVIDERS
        for p in providers:
            models = self.get_models(p)
            for mid, m in models.items():
                if capability.value in (m.capabilities or []):
                    result.append(f"{p}/{mid}")

        return result

    def get_model_info(self, provider: str, model_id: str) -> Optional[ModelInfo]:
        """获取单个模型的详细信息"""
        models = self.get_models(provider)
        return models.get(model_id)

    def summary(self) -> str:
        """输出当前模型注册表摘要"""
        lines = []
        lines.append(f"当前锁定厂家: {self.active_provider or '无（自动选择）'}")
        lines.append(f"成本偏好: {self.cost_preference}")
        lines.append("")

        for p in self.SUPPORTED_PROVIDERS:
            models = self.get_models(p)
            if not models:
                lines.append(f"[{p}] 未配置或无可用模型")
                continue
            lines.append(f"[{p}] {len(models)} 个模型:")
            for mid, m in models.items():
                caps = ", ".join(m.capabilities or [])
                tokens = f"{m.input_token_limit or '?'}t" if m.input_token_limit else ""
                lines.append(f"  - {mid} [{m.cost_tier}] {tokens} ({caps})")

        return "\n".join(lines)


# 全局单例
_registry: Optional[ModelRegistry] = None


def get_registry() -> ModelRegistry:
    """获取全局 ModelRegistry 单例"""
    global _registry
    if _registry is None:
        _registry = ModelRegistry()
    return _registry


def set_active_provider(provider: str):
    """设置用户选择的厂家"""
    get_registry().set_provider(provider)
