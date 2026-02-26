"""
OmniCore LiteLLM 统一接口封装
支持无缝切换 OpenAI, Anthropic, Gemini 等模型
"""
import json
from typing import Optional, List, Dict, Any
from litellm import completion, acompletion
import litellm
from pydantic import BaseModel

from config.settings import settings
from utils.logger import logger, log_agent_action

# 自动丢弃模型不支持的参数（如 GPT-5 不支持 temperature）
litellm.drop_params = True


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
    """

    def __init__(self, model: Optional[str] = None):
        """
        初始化 LLM 客户端

        Args:
            model: 模型名称，默认使用配置中的 DEFAULT_MODEL
        """
        self.model = model or settings.DEFAULT_MODEL
        self._setup_api_keys()

    def _setup_api_keys(self):
        """设置 API Keys（LiteLLM 会自动从环境变量读取）"""
        import os
        if settings.OPENAI_API_KEY:
            os.environ["OPENAI_API_KEY"] = settings.OPENAI_API_KEY
        if settings.ANTHROPIC_API_KEY:
            os.environ["ANTHROPIC_API_KEY"] = settings.ANTHROPIC_API_KEY
        if settings.GEMINI_API_KEY:
            os.environ["GEMINI_API_KEY"] = settings.GEMINI_API_KEY
        if settings.DEEPSEEK_API_KEY:
            os.environ["DEEPSEEK_API_KEY"] = settings.DEEPSEEK_API_KEY

    def _safe_max_tokens(self, requested: int) -> int:
        """根据模型限制返回安全的 max_tokens 值"""
        model_lower = self.model.lower()
        if "deepseek" in model_lower:
            return min(requested, 8192)
        if "claude" in model_lower:
            return min(requested, 8192)
        return requested

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 16000,
        json_mode: bool = False,
    ) -> LLMResponse:
        """
        同步聊天接口

        Args:
            messages: 消息列表 [{"role": "user", "content": "..."}]
            temperature: 温度参数
            max_tokens: 最大 token 数
            json_mode: 是否强制 JSON 输出

        Returns:
            LLMResponse: 统一响应结构
        """
        try:
            kwargs = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": self._safe_max_tokens(max_tokens),
            }

            # JSON 模式（部分模型支持）
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}

            response = completion(**kwargs)

            content = response.choices[0].message.content
            if not content:
                refusal = getattr(response.choices[0].message, 'refusal', None)
                if refusal:
                    logger.error(f"LLM 拒绝回答: {refusal}")
                    content = f'{{"intent": "unknown", "confidence": 0, "reasoning": "模型拒绝: {refusal}", "tasks": [], "is_high_risk": false}}'
                else:
                    logger.error(f"LLM 返回空内容, finish_reason: {response.choices[0].finish_reason}")
                    content = '{"intent": "unknown", "confidence": 0, "reasoning": "模型返回空内容", "tasks": [], "is_high_risk": false}'

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

        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            raise

    async def achat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 16000,
        json_mode: bool = False,
    ) -> LLMResponse:
        """
        异步聊天接口
        """
        try:
            kwargs = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": self._safe_max_tokens(max_tokens),
            }

            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}

            response = await acompletion(**kwargs)

            return LLMResponse(
                content=response.choices[0].message.content,
                model=response.model,
                usage={
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                },
                raw_response=response,
            )

        except Exception as e:
            logger.error(f"异步 LLM 调用失败: {e}")
            raise

    def chat_with_system(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.7,
        max_tokens: int = 16000,
        json_mode: bool = False,
    ) -> LLMResponse:
        """
        带系统提示的聊天（便捷方法）
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        return self.chat(messages, temperature, max_tokens, json_mode)

    def parse_json_response(self, response: LLMResponse) -> Dict[str, Any]:
        """
        解析 JSON 格式的响应
        """
        import re
        content = response.content.strip()

        # 尝试提取 JSON 块
        if "```json" in content:
            start = content.find("```json") + 7
            end = content.find("```", start)
            content = content[start:end].strip()
        elif "```" in content:
            start = content.find("```") + 3
            end = content.find("```", start)
            content = content[start:end].strip()

        # 尝试直接解析
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # 尝试提取第一个完整的 JSON 对象
        try:
            # 找到第一个 { 和匹配的 }
            brace_count = 0
            start_idx = -1
            end_idx = -1

            for i, char in enumerate(content):
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
                json_str = content[start_idx:end_idx]
                return json.loads(json_str)
        except json.JSONDecodeError:
            pass

        # 尝试用正则提取
        try:
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass

        logger.error(f"JSON 解析失败")
        logger.error(f"原始内容: {content[:500]}")
        raise ValueError(f"无法解析 JSON: {content[:200]}")
