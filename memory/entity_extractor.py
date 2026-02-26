"""
OmniCore 实体抽取器
从对话和任务结果中提取关键实体
"""
from typing import List, Dict, Any, Optional

from core.llm import LLMClient
from utils.logger import log_agent_action, logger


ENTITY_EXTRACTION_PROMPT = """你是一个实体抽取专家。请从给定文本中提取关键实体。

## 需要提取的实体类型：
- PERSON: 人名
- ORGANIZATION: 组织/公司名
- LOCATION: 地点
- URL: 网址
- FILE_PATH: 文件路径
- DATE: 日期时间
- PRODUCT: 产品名称
- KEYWORD: 关键词/主题

## 输出格式（JSON）：
{
    "entities": [
        {
            "text": "实体文本",
            "type": "实体类型",
            "confidence": 0.95
        }
    ],
    "summary": "文本摘要（一句话）"
}

只提取明确出现在文本中的实体，不要推测。
"""


class EntityExtractor:
    """
    实体抽取器
    从文本中提取结构化实体信息
    """

    def __init__(self, llm_client: LLMClient = None):
        self.llm = llm_client or LLMClient()
        self.name = "EntityExtractor"

    def extract(self, text: str) -> Dict[str, Any]:
        """
        从文本中提取实体

        Args:
            text: 输入文本

        Returns:
            包含实体列表的字典
        """
        if not text or len(text.strip()) < 10:
            return {"entities": [], "summary": ""}

        log_agent_action(self.name, "开始实体抽取", f"文本长度: {len(text)}")

        try:
            response = self.llm.chat_with_system(
                system_prompt=ENTITY_EXTRACTION_PROMPT,
                user_message=f"请从以下文本中提取实体：\n\n{text[:2000]}",
                temperature=0.2,
                max_tokens=1000,
                json_mode=True,
            )

            result = self.llm.parse_json_response(response)
            entities = result.get("entities", [])
            log_agent_action(self.name, f"抽取完成", f"发现 {len(entities)} 个实体")
            return result

        except Exception as e:
            logger.error(f"实体抽取失败: {e}")
            return {"entities": [], "summary": "", "error": str(e)}

    def extract_from_task_result(
        self,
        task_description: str,
        task_result: Any,
    ) -> Dict[str, Any]:
        """
        从任务结果中提取实体

        Args:
            task_description: 任务描述
            task_result: 任务结果

        Returns:
            实体信息
        """
        combined_text = f"任务: {task_description}\n结果: {str(task_result)}"
        return self.extract(combined_text)

    def batch_extract(self, texts: List[str]) -> List[Dict[str, Any]]:
        """
        批量提取实体

        Args:
            texts: 文本列表

        Returns:
            实体结果列表
        """
        results = []
        for text in texts:
            result = self.extract(text)
            results.append(result)
        return results
