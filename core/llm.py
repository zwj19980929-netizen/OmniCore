"""
OmniCore LiteLLM 统一接口封装
支持无缝切换 OpenAI, Anthropic, Gemini, DeepSeek, Kimi 等模型
支持按能力自动选择子模型 + 多模态调用
"""
import json
import base64
import asyncio
import re
from typing import Optional, List, Dict, Any, Union
from pathlib import Path
import os

import time as _time

from config.settings import settings
from utils.logger import logger, log_agent_action
from utils.text import sanitize_text, sanitize_value
from utils.structured_logger import get_structured_logger
from litellm import completion, acompletion
import litellm
import yaml
import requests
from pydantic import BaseModel

# 自动丢弃模型不支持的参数（如 GPT-5 不支持 temperature）
litellm.drop_params = True
# 抑制 litellm 对未知模型的 "Provider List" 调试输出
litellm.suppress_debug_info = True


class LLMResponse(BaseModel):
    """LLM 响应结构"""
    content: str
    model: str
    usage: Dict[str, int]
    raw_response: Optional[Any] = None


class LLMClient:
    """
    LiteLLM 统一客户端
    封装多模型调用，提供一致的接口
    支持按能力自动选择子模型、Kimi/Gemini 接入、多模态调用
    """

    # 厂家到 API Base 的映射
    PROVIDER_API_BASE = {
        "kimi": "https://api.moonshot.cn/v1",
        "moonshot": "https://api.moonshot.cn/v1",
        "openai": "https://api.openai.com/v1",
        "deepseek": "https://api.deepseek.com",
        "minimax": "https://api.minimaxi.com/v1",
    }
    MINIMAX_ALT_BASE = {
        "https://api.minimaxi.com/v1": "https://api.minimax.io/v1",
        "https://api.minimax.io/v1": "https://api.minimaxi.com/v1",
    }

    def __init__(
        self,
        model: Optional[str] = None,
        capability: Optional[str] = None,
        provider: Optional[str] = None,
    ):
        """
        初始化 LLM 客户端

        Args:
            model: 模型名称，如 "gemini/gemini-2.5-pro" 或 "gpt-4o"
            capability: 按能力自动选择模型，如 "vision", "image_gen", "stt"
            provider: 限定厂家，如 "gemini", "kimi", "minimax"
        """
        if model:
            self.model = model
        elif capability:
            from core.model_registry import get_registry, ModelCapability
            registry = get_registry()
            if provider:
                registry.set_provider(provider)
            cap_enum = ModelCapability(capability)
            resolved = registry.get_model_for_capability(cap_enum)
            if not resolved:
                raise ValueError(f"没有找到支持 {capability} 的模型")
            self.model = resolved
        else:
            self.model = settings.DEFAULT_MODEL

        self.provider_config = self._load_provider_config()
        self._setup_api_keys()

    @classmethod
    def for_capability(cls, capability: str, provider: str = None) -> "LLMClient":
        """工厂方法：根据能力创建客户端"""
        return cls(capability=capability, provider=provider)

    @classmethod
    def for_vision(cls, provider: str = None) -> "LLMClient":
        """快捷方法：创建视觉模型客户端"""
        return cls(capability="vision", provider=provider)

    @classmethod
    def for_image_gen(cls, provider: str = None) -> "LLMClient":
        """快捷方法：创建图片生成客户端"""
        return cls(capability="image_gen", provider=provider)

    def _setup_api_keys(self):
        """设置 API Keys（LiteLLM 会自动从环境变量读取）"""
        if settings.OPENAI_API_KEY:
            os.environ["OPENAI_API_KEY"] = settings.OPENAI_API_KEY
        if settings.ANTHROPIC_API_KEY:
            os.environ["ANTHROPIC_API_KEY"] = settings.ANTHROPIC_API_KEY
        if settings.GEMINI_API_KEY:
            os.environ["GEMINI_API_KEY"] = settings.GEMINI_API_KEY
        if settings.DEEPSEEK_API_KEY:
            os.environ["DEEPSEEK_API_KEY"] = settings.DEEPSEEK_API_KEY
        kimi_key = getattr(settings, "KIMI_API_KEY", "")
        if kimi_key:
            os.environ["KIMI_API_KEY"] = kimi_key
        minimax_key = getattr(settings, "MINIMAX_API_KEY", "")
        if minimax_key:
            os.environ["MINIMAX_API_KEY"] = minimax_key
        zhipu_key = getattr(settings, "ZHIPU_API_KEY", "")
        if zhipu_key:
            os.environ["ZHIPU_API_KEY"] = zhipu_key

    def _load_provider_config(self) -> Dict[str, Any]:
        """从 models.yaml 加载 provider_config"""
        config_path = Path(settings.MODELS_CONFIG_PATH)
        if not config_path.exists():
            return {}
        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            provider_cfg = data.get("provider_config", {})
            return provider_cfg if isinstance(provider_cfg, dict) else {}
        except Exception as e:
            logger.warning(f"读取模型配置失败: {e}")
            return {}

    def _get_provider_api_key(self, provider: str) -> str:
        """按 provider 获取 API key（优先 settings，其次 provider_config 指定 env）"""
        provider = provider.lower()
        if provider == "gemini":
            if settings.GEMINI_API_KEY:
                return settings.GEMINI_API_KEY
        elif provider in ("kimi", "moonshot"):
            kimi_key = getattr(settings, "KIMI_API_KEY", "")
            if kimi_key:
                return kimi_key
        elif provider == "openai":
            if settings.OPENAI_API_KEY:
                return settings.OPENAI_API_KEY
        elif provider == "deepseek":
            if settings.DEEPSEEK_API_KEY:
                return settings.DEEPSEEK_API_KEY
        elif provider == "minimax":
            minimax_key = getattr(settings, "MINIMAX_API_KEY", "")
            if minimax_key:
                return minimax_key
        elif provider == "zhipu":
            zhipu_key = getattr(settings, "ZHIPU_API_KEY", "")
            if zhipu_key:
                return zhipu_key

        cfg = self.provider_config.get(provider, {})
        env_name = cfg.get("api_key_env", "")
        if isinstance(env_name, str) and env_name and env_name.isupper() and "_" in env_name:
            return os.getenv(env_name, "")
        return ""

    def _get_provider_from_model(self) -> str:
        """从模型名解析厂家"""
        if "/" in self.model:
            return self.model.split("/")[0]
        model_lower = self.model.lower()
        if "gemini" in model_lower or "imagen" in model_lower:
            return "gemini"
        if "moonshot" in model_lower or "kimi" in model_lower:
            return "kimi"
        if "deepseek" in model_lower:
            return "deepseek"
        if "minimax" in model_lower or model_lower.startswith("abab"):
            return "minimax"
        if "glm" in model_lower or "zhipu" in model_lower:
            return "zhipu"
        return "openai"

    def _get_litellm_model(self) -> str:
        """转换为 LiteLLM 识别的模型格式"""
        provider = self._get_provider_from_model()
        model_id = self.model.split("/")[-1] if "/" in self.model else self.model

        # OpenAI 原生模型不加前缀（litellm 直接认识 gpt-* 系列）
        # 只有 kimi/moonshot/minimax 等 OpenAI 兼容 API 才加 openai/ 前缀
        if provider == "openai":
            return model_id
        if provider in ("kimi", "moonshot", "minimax", "zhipu"):
            return f"openai/{model_id}"
        if provider == "gemini":
            return f"gemini/{model_id}"
        if provider == "deepseek":
            return f"deepseek/{model_id}"
        return model_id

    def _get_extra_kwargs(self) -> Dict[str, Any]:
        """获取额外的 LiteLLM 参数（如 api_base, api_key）"""
        provider = self._get_provider_from_model()
        kwargs: Dict[str, Any] = {}

        cfg = self.provider_config.get(provider, {})
        api_base = cfg.get("api_base")
        if api_base:
            kwargs["api_base"] = api_base
        elif provider == "openai" and settings.OPENAI_API_BASE:
            kwargs["api_base"] = settings.OPENAI_API_BASE
        elif provider in self.PROVIDER_API_BASE:
            kwargs["api_base"] = self.PROVIDER_API_BASE[provider]

        api_key = self._get_provider_api_key(provider)
        if api_key:
            # 对代理或 OpenAI 兼容接口显式传 key，减少环境变量依赖问题
            kwargs["api_key"] = api_key

        return kwargs

    def _should_use_gemini_query_mode(self) -> bool:
        """
        对于需要 query key 鉴权的 Gemini 代理，绕开 LiteLLM 的 custom api_base 认证差异。
        """
        provider = self._get_provider_from_model()
        if provider != "gemini":
            return False
        cfg = self.provider_config.get("gemini", {})
        return str(cfg.get("auth_mode", "")).lower() == "query_key" and bool(cfg.get("api_base"))

    def _gemini_message_to_contents(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        OpenAI-style messages -> Gemini contents.
        支持文本和 data URL 图片（inline_data）。
        """
        contents: List[Dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role", "user")
            gemini_role = "model" if role == "assistant" else "user"
            content = msg.get("content", "")

            parts: List[Dict[str, Any]] = []
            if isinstance(content, str):
                parts.append({"text": content})
            elif isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type")
                    if item_type == "text":
                        parts.append({"text": item.get("text", "")})
                    elif item_type == "image_url":
                        image_url = (item.get("image_url") or {}).get("url", "")
                        if image_url.startswith("data:") and ";base64," in image_url:
                            try:
                                prefix, b64 = image_url.split(";base64,", 1)
                                mime = prefix.replace("data:", "")
                                parts.append({
                                    "inline_data": {
                                        "mime_type": mime,
                                        "data": b64,
                                    }
                                })
                            except Exception:
                                continue

            if parts:
                contents.append({"role": gemini_role, "parts": parts})

        if not contents:
            contents = [{"role": "user", "parts": [{"text": ""}]}]
        return contents

    def _chat_gemini_query_mode(
        self,
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> LLMResponse:
        cfg = self.provider_config.get("gemini", {})
        api_base = str(cfg.get("api_base", "")).rstrip("/")
        api_key = self._get_provider_api_key("gemini")
        if not api_base or not api_key:
            raise ValueError("Gemini query mode 缺少 api_base 或 api_key")

        model_id = self.model.split("/")[-1] if "/" in self.model else self.model
        url = f"{api_base}/models/{model_id}:generateContent"
        params = {"key": api_key}

        generation_config: Dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": self._safe_max_tokens(max_tokens),
        }
        if json_mode:
            generation_config["responseMimeType"] = "application/json"

        payload: Dict[str, Any] = {
            "contents": self._gemini_message_to_contents(messages),
            "generationConfig": generation_config,
        }

        resp = requests.post(
            url,
            params=params,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()

        candidates = data.get("candidates", [])
        content = ""
        if candidates:
            parts = ((candidates[0].get("content") or {}).get("parts") or [])
            texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
            content = "".join(texts).strip()

        if not content:
            content = '{"intent":"unknown","confidence":0,"reasoning":"Gemini 返回空内容","tasks":[],"is_high_risk":false}'

        usage_meta = data.get("usageMetadata", {})
        usage = {
            "prompt_tokens": usage_meta.get("promptTokenCount", 0),
            "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
            "total_tokens": usage_meta.get("totalTokenCount", 0),
        }

        return LLMResponse(
            content=content,
            model=data.get("modelVersion", self.model),
            usage=usage,
            raw_response=data,
        )

    def _safe_max_tokens(self, requested: int) -> int:
        """根据模型限制返回安全的 max_tokens 值"""
        known_limit = self._get_known_max_output_tokens()
        if known_limit:
            return min(requested, known_limit)

        model_lower = self.model.lower()
        if "deepseek" in model_lower:
            return min(requested, 8192)
        if "claude" in model_lower:
            return min(requested, 8192)
        if "moonshot" in model_lower or "kimi" in model_lower:
            return min(requested, 8192)
        return requested

    def _get_known_max_output_tokens(self) -> Optional[int]:
        """尝试从 LiteLLM 元数据或本地经验规则推断输出 token 上限。"""
        provider = self._get_provider_from_model()
        model_id = self.model.split("/")[-1] if "/" in self.model else self.model
        model_id_lower = model_id.lower()

        if provider == "openai" and model_id_lower.startswith("gpt-5"):
            return 4096

        lookup_keys = [
            self._get_litellm_model(),
            self.model,
            model_id,
            model_id_lower,
            f"{provider}/{model_id}",
            f"{provider}/{model_id_lower}",
        ]

        seen = set()
        for key in lookup_keys:
            if not key or key in seen:
                continue
            seen.add(key)

            model_info = litellm.model_cost.get(key)
            if not isinstance(model_info, dict):
                continue

            for field in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
                value = model_info.get(field)
                if isinstance(value, int) and value > 0:
                    return value

        return None

    @staticmethod
    def _extract_completion_token_limit(error: Exception) -> Optional[int]:
        """从上游报错中提取允许的 completion token 上限。"""
        message = str(error or "")
        patterns = (
            r"supports at most (\d+) completion tokens",
            r"supports at most (\d+) output tokens",
            r"max[_ ]tokens .*?supports at most (\d+)",
        )

        for pattern in patterns:
            match = re.search(pattern, message, re.IGNORECASE)
            if not match:
                continue
            try:
                limit = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if limit > 0:
                return limit

        return None

    def _maybe_get_reduced_max_tokens_kwargs(
        self,
        kwargs: Dict[str, Any],
        error: Exception,
    ) -> Optional[Dict[str, Any]]:
        """当上游明确返回 max_tokens 上限时，构造一次降级重试参数。"""
        current_limit = kwargs.get("max_tokens")
        if not isinstance(current_limit, int) or current_limit <= 0:
            return None

        supported_limit = self._extract_completion_token_limit(error)
        if supported_limit is None or supported_limit >= current_limit:
            return None

        retry_kwargs = dict(kwargs)
        retry_kwargs["max_tokens"] = supported_limit
        return retry_kwargs

    @classmethod
    def _maybe_get_minimax_fallback_base(cls, *, provider: str, api_base: str, error: Exception) -> str:
        if provider != "minimax":
            return ""
        message = str(error or "")
        if "invalid api key (2049)" not in message:
            return ""
        current = str(api_base or "").rstrip("/")
        return cls.MINIMAX_ALT_BASE.get(current, "")

    def _build_chat_kwargs(
        self,
        clean_messages: list,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> dict:
        """构建 LLM 调用参数（chat 与 achat 共用）。"""
        kwargs = {
            "model": self._get_litellm_model(),
            "messages": clean_messages,
            "temperature": temperature,
            "max_tokens": self._safe_max_tokens(max_tokens),
            "timeout": 120,
            **self._get_extra_kwargs(),
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return kwargs

    def _build_llm_response(self, response, kwargs: dict, call_start: float) -> "LLMResponse":
        """从原始 LLM 响应构建 LLMResponse，处理空内容/拒绝，记录结构化日志（chat 与 achat 共用）。"""
        content = sanitize_text(response.choices[0].message.content or "")
        if not content:
            refusal = getattr(response.choices[0].message, "refusal", None)
            if refusal:
                logger.error(f"LLM 拒绝回答: {refusal}")
                content = (
                    f'{{"intent": "unknown", "confidence": 0, '
                    f'"reasoning": "模型拒绝: {refusal}", "tasks": [], "is_high_risk": false}}'
                )
            else:
                logger.error(f"LLM 返回空内容, finish_reason: {response.choices[0].finish_reason}")
                content = (
                    '{"intent": "unknown", "confidence": 0, '
                    '"reasoning": "模型返回空内容", "tasks": [], "is_high_risk": false}'
                )

        call_duration_ms = (_time.time() - call_start) * 1000
        get_structured_logger().log_llm_call(
            model=response.model or kwargs.get("model", "unknown"),
            tokens_in=response.usage.prompt_tokens,
            tokens_out=response.usage.completion_tokens,
            duration_ms=call_duration_ms,
        )
        return LLMResponse(
            content=content,
            model=response.model,
            usage={
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            },
            raw_response=response,
        )

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        """
        同步聊天接口（支持所有厂家）

        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大 tokens 数，None 时使用配置的默认值
            json_mode: 是否启用 JSON 模式
        """
        from config.settings import settings

        # 如果没有指定 max_tokens，使用配置的默认值
        if max_tokens is None:
            max_tokens = settings.LLM_MAX_TOKENS

        try:
            clean_messages = sanitize_value(messages)

            if self._should_use_gemini_query_mode():
                return self._chat_gemini_query_mode(
                    messages=clean_messages,  # type: ignore[arg-type]
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                )

            kwargs = self._build_chat_kwargs(clean_messages, temperature, max_tokens, json_mode)
            logger.info(f"LLM 调用开始: model={kwargs['model']}, max_tokens={kwargs['max_tokens']}, timeout=120s")
            _call_start = _time.time()

            max_retries = 3
            last_error = None

            for attempt in range(max_retries):
                try:
                    if attempt > 0:
                        logger.info(f"LLM 调用重试 {attempt}/{max_retries}...")
                    response = completion(**kwargs)
                    break
                except Exception as first_error:
                    last_error = first_error
                    error_str = str(first_error).lower()

                    is_network_error = any(keyword in error_str for keyword in [
                        "peer closed connection",
                        "incomplete chunked read",
                        "connection reset",
                        "connection aborted",
                        "timeout",
                        "timed out",
                    ])

                    if is_network_error and attempt < max_retries - 1:
                        import time
                        wait_time = (attempt + 1) * 2
                        logger.warning(
                            f"LLM 网络连接错误，{wait_time}秒后重试 ({attempt + 1}/{max_retries}): {error_str[:100]}"
                        )
                        time.sleep(wait_time)
                        continue

                    retry_kwargs = self._maybe_get_reduced_max_tokens_kwargs(kwargs, first_error)
                    if retry_kwargs is not None:
                        logger.warning(
                            "LLM max_tokens 超出模型限制，自动降级重试: "
                            f"{kwargs.get('max_tokens')} -> {retry_kwargs['max_tokens']}"
                        )
                        response = completion(**retry_kwargs)
                        break

                    fallback_base = self._maybe_get_minimax_fallback_base(
                        provider=self._get_provider_from_model(),
                        api_base=str(kwargs.get("api_base", "") or ""),
                        error=first_error,
                    )
                    if fallback_base:
                        retry_kwargs = dict(kwargs)
                        retry_kwargs["api_base"] = fallback_base
                        logger.warning(
                            "MiniMax 鉴权失败，自动切换备用官方域名重试: "
                            f"{kwargs.get('api_base')} -> {fallback_base}"
                        )
                        response = completion(**retry_kwargs)
                        break

                    raise
            else:
                raise last_error if last_error else Exception("LLM 调用失败")

            return self._build_llm_response(response, kwargs, _call_start)

        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            raise

    async def achat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        """
        异步聊天接口（支持所有厂家）

        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大 tokens 数，None 时使用配置的默认值
            json_mode: 是否启用 JSON 模式
        """
        from config.settings import settings

        # 如果没有指定 max_tokens，使用配置的默认值
        if max_tokens is None:
            max_tokens = settings.LLM_MAX_TOKENS

        try:
            clean_messages = sanitize_value(messages)

            if self._should_use_gemini_query_mode():
                return await asyncio.to_thread(
                    self._chat_gemini_query_mode,
                    clean_messages,  # type: ignore[arg-type]
                    temperature,
                    max_tokens,
                    json_mode,
                )

            kwargs = self._build_chat_kwargs(clean_messages, temperature, max_tokens, json_mode)
            logger.info(f"LLM 异步调用开始: model={kwargs['model']}, max_tokens={kwargs['max_tokens']}, timeout=120s")
            _call_start = _time.time()

            max_retries = 3
            last_error = None

            for attempt in range(max_retries):
                try:
                    response = await acompletion(**kwargs)
                    break
                except Exception as first_error:
                    last_error = first_error
                    error_str = str(first_error).lower()

                    is_network_error = any(keyword in error_str for keyword in [
                        "peer closed connection",
                        "incomplete chunked read",
                        "connection reset",
                        "connection aborted",
                        "timeout",
                        "timed out",
                    ])

                    if is_network_error and attempt < max_retries - 1:
                        wait_time = (attempt + 1) * 2
                        logger.warning(
                            f"LLM 网络连接错误，{wait_time}秒后重试 ({attempt + 1}/{max_retries}): {error_str[:100]}"
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    retry_kwargs = self._maybe_get_reduced_max_tokens_kwargs(kwargs, first_error)
                    if retry_kwargs is not None:
                        logger.warning(
                            "LLM max_tokens 超出模型限制，自动降级重试: "
                            f"{kwargs.get('max_tokens')} -> {retry_kwargs['max_tokens']}"
                        )
                        response = await acompletion(**retry_kwargs)
                        break

                    fallback_base = self._maybe_get_minimax_fallback_base(
                        provider=self._get_provider_from_model(),
                        api_base=str(kwargs.get("api_base", "") or ""),
                        error=first_error,
                    )
                    if fallback_base:
                        retry_kwargs = dict(kwargs)
                        retry_kwargs["api_base"] = fallback_base
                        logger.warning(
                            "MiniMax 鉴权失败，自动切换备用官方域名重试: "
                            f"{kwargs.get('api_base')} -> {fallback_base}"
                        )
                        response = await acompletion(**retry_kwargs)
                        break

                    raise
            else:
                raise last_error if last_error else Exception("LLM 调用失败")

            return self._build_llm_response(response, kwargs, _call_start)

        except Exception as e:
            logger.error(f"异步 LLM 调用失败: {e}")
            raise

    def chat_with_system(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.7,
        max_tokens: int = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        """
        带系统提示的聊天（便捷方法）

        Args:
            system_prompt: 系统提示
            user_message: 用户消息
            temperature: 温度参数
            max_tokens: 最大 tokens 数，None 时使用配置的默认值
            json_mode: 是否启用 JSON 模式
        """
        messages = [
            {"role": "system", "content": sanitize_text(system_prompt or "")},
            {"role": "user", "content": sanitize_text(user_message or "")},
        ]
        return self.chat(messages, temperature, max_tokens, json_mode)

    def parse_json_response(self, response: LLMResponse) -> Dict[str, Any]:
        """
        解析 JSON 格式的响应。
        尝试顺序: Markdown 代码块提取 → 大括号匹配 → 正则提取。
        对原始内容和双大括号标准化版本各尝试一次。
        """
        content = response.content.strip()
        candidates = [content]
        normalized_braces = content.replace("{{", "{").replace("}}", "}")
        if normalized_braces != content:
            candidates.append(normalized_braces)

        def _try_parse(candidate: str):
            block = candidate
            if "```json" in block:
                start = block.find("```json") + 7
                end = block.find("```", start)
                block = block[start:end].strip() if end != -1 else block[start:].strip()
            elif "```" in block:
                start = block.find("```") + 3
                end = block.find("```", start)
                block = block[start:end].strip() if end != -1 else block[start:].strip()

            try:
                return json.loads(block)
            except json.JSONDecodeError:
                pass

            try:
                brace_count = 0
                start_idx = -1
                end_idx = -1
                for i, char in enumerate(block):
                    if char == '{':
                        if start_idx == -1:
                            start_idx = i
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0 and start_idx != -1:
                            end_idx = i + 1
                            break
                if start_idx != -1 and end_idx != -1:
                    return json.loads(block[start_idx:end_idx])
            except json.JSONDecodeError:
                pass

            try:
                json_match = re.search(r'\{[\s\S]*\}', block)
                if json_match:
                    return json.loads(json_match.group(0))
            except json.JSONDecodeError:
                pass
            return None

        for candidate in candidates:
            parsed = _try_parse(candidate)
            if parsed is not None:
                return parsed

        logger.error("JSON 解析失败")
        logger.error(f"原始内容: {content[:500]}")
        raise ValueError(f"无法解析 JSON: {content[:200]}")

    # ========== 多模态扩展方法 ==========

    def _prepare_image_content(self, image: Union[str, Path, bytes]) -> Dict:
        """准备图片内容（URL 或 base64）"""
        if isinstance(image, bytes):
            b64 = base64.b64encode(image).decode()
            return {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"}
            }
        elif isinstance(image, Path) or (isinstance(image, str) and not image.startswith("http")):
            path = Path(image)
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            suffix = path.suffix.lower().lstrip(".")
            mime_map = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "gif": "gif", "webp": "webp", "bmp": "bmp"}
            mime = mime_map.get(suffix, "png")
            return {
                "type": "image_url",
                "image_url": {"url": f"data:image/{mime};base64,{b64}"}
            }
        else:
            return {
                "type": "image_url",
                "image_url": {"url": image}
            }

    def chat_with_image(
        self,
        text: str,
        image: Union[str, Path, bytes],
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """
        带图片的对话（视觉理解）

        Args:
            text: 文本提示
            image: 图片路径、URL 或 bytes
        """
        image_content = self._prepare_image_content(image)
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                image_content,
            ]
        }]
        return self.chat(messages, temperature, max_tokens)

    def generate_image(
        self,
        prompt: str,
        size: str = "1024x1024",
        quality: str = "standard",
        n: int = 1,
    ) -> List[str]:
        """
        图片生成

        Returns:
            生成的图片 URL 列表
        """
        from openai import OpenAI

        provider = self._get_provider_from_model()
        model_id = self.model.split("/")[-1] if "/" in self.model else self.model

        if provider == "openai":
            client = OpenAI(api_key=settings.OPENAI_API_KEY)
            response = client.images.generate(
                model=model_id,
                prompt=prompt,
                size=size,
                quality=quality,
                n=n,
            )
            return [img.url for img in response.data]

        raise ValueError(f"{provider} 暂不支持图片生成，请使用 openai/dall-e-3")

    def transcribe(
        self,
        audio_path: Union[str, Path],
        language: str = None,
    ) -> str:
        """
        语音识别（STT）

        Args:
            audio_path: 音频文件路径
            language: 语言代码（如 "zh", "en"）

        Returns:
            识别的文本
        """
        from openai import OpenAI

        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        kwargs: Dict[str, Any] = {"model": "whisper-1"}
        if language:
            kwargs["language"] = language

        with open(audio_path, "rb") as f:
            kwargs["file"] = f
            response = client.audio.transcriptions.create(**kwargs)

        return response.text

    def speak(
        self,
        text: str,
        output_path: Union[str, Path],
        voice: str = "alloy",
        model: str = "tts-1",
    ) -> Path:
        """
        语音合成（TTS）

        Args:
            text: 要转换的文本
            output_path: 输出音频路径
            voice: 声音类型 (alloy, echo, fable, onyx, nova, shimmer)
            model: tts-1 或 tts-1-hd

        Returns:
            输出文件路径
        """
        from openai import OpenAI

        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.audio.speech.create(
            model=model,
            voice=voice,
            input=text,
        )

        output_path = Path(output_path)
        response.stream_to_file(output_path)
        return output_path
