"""
OmniCore ChromaDB 向量存储
本地记忆中心，存储用户习惯和历史上下文
"""
import uuid
from typing import List, Dict, Any, Optional
from pathlib import Path
from datetime import datetime

import chromadb
from chromadb.config import Settings as ChromaSettings

from config.settings import settings
from utils.logger import log_agent_action, logger, log_success
from utils.text import sanitize_text, sanitize_value


class ChromaMemory:
    """
    ChromaDB 向量记忆存储
    用于存储和检索历史对话、任务结果、用户偏好
    """

    def __init__(self, collection_name: str = "omnicore_memory"):
        self.name = "ChromaMemory"
        self.collection_name = collection_name
        self._client = None
        self._collection = None
        self._init_client()

    def _init_client(self):
        """初始化 ChromaDB 客户端"""
        persist_dir = settings.CHROMA_PERSIST_DIR
        persist_dir.mkdir(parents=True, exist_ok=True)

        self._client = chromadb.PersistentClient(
            path=str(persist_dir),
            settings=ChromaSettings(
                anonymized_telemetry=False,
            ),
        )

        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            metadata={"description": "OmniCore 记忆存储"},
        )

        log_agent_action(self.name, "初始化完成", f"集合: {self.collection_name}")

    def add_memory(
        self,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        memory_type: str = "general",
    ) -> str:
        """
        添加记忆

        Args:
            content: 记忆内容
            metadata: 元数据
            memory_type: 记忆类型 (general, task, preference, entity)

        Returns:
            记忆 ID
        """
        clean_content = sanitize_text(content or "")
        memory_id = f"mem_{uuid.uuid4().hex[:12]}"

        meta = {
            "type": memory_type,
            "timestamp": datetime.now().isoformat(),
            "content_length": len(clean_content),
        }
        if metadata:
            meta.update(sanitize_value(metadata))

        self._collection.add(
            ids=[memory_id],
            documents=[clean_content],
            metadatas=[meta],
        )

        log_agent_action(self.name, "添加记忆", f"ID: {memory_id}, 类型: {memory_type}")
        return memory_id

    def search_memory(
        self,
        query: str,
        n_results: int = 5,
        memory_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        搜索相关记忆

        Args:
            query: 查询文本
            n_results: 返回结果数量
            memory_type: 过滤记忆类型

        Returns:
            相关记忆列表
        """
        clean_query = sanitize_text(query or "")
        log_agent_action(self.name, "搜索记忆", clean_query[:30])

        where_filter = None
        if memory_type:
            where_filter = {"type": memory_type}

        results = self._collection.query(
            query_texts=[clean_query],
            n_results=n_results,
            where=where_filter,
        )

        memories = []
        if results and results["documents"]:
            for i, doc in enumerate(results["documents"][0]):
                memories.append({
                    "id": results["ids"][0][i],
                    "content": sanitize_text(doc or ""),
                    "metadata": sanitize_value(
                        results["metadatas"][0][i] if results["metadatas"] else {}
                    ),
                    "distance": results["distances"][0][i] if results["distances"] else None,
                })

        return memories

    def get_recent_memories(
        self,
        limit: int = 10,
        memory_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        获取最近的记忆

        Args:
            limit: 数量限制
            memory_type: 过滤类型

        Returns:
            记忆列表
        """
        where_filter = None
        if memory_type:
            where_filter = {"type": memory_type}

        results = self._collection.get(
            where=where_filter,
            limit=limit,
        )

        memories = []
        if results and results["documents"]:
            for i, doc in enumerate(results["documents"]):
                memories.append({
                    "id": results["ids"][i],
                    "content": doc,
                    "metadata": results["metadatas"][i] if results["metadatas"] else {},
                })

        return memories

    def delete_memory(self, memory_id: str) -> bool:
        """删除指定记忆"""
        try:
            self._collection.delete(ids=[memory_id])
            log_agent_action(self.name, "删除记忆", memory_id)
            return True
        except Exception as e:
            logger.error(f"删除记忆失败: {e}")
            return False

    def clear_all(self) -> bool:
        """清空所有记忆（危险操作）"""
        try:
            self._client.delete_collection(self.collection_name)
            self._collection = self._client.get_or_create_collection(
                name=self.collection_name,
            )
            log_success("记忆已清空")
            return True
        except Exception as e:
            logger.error(f"清空记忆失败: {e}")
            return False

    def save_task_result(
        self,
        task_description: str,
        result: Any,
        success: bool,
    ) -> str:
        """
        保存任务执行结果到记忆

        Args:
            task_description: 任务描述
            result: 执行结果
            success: 是否成功

        Returns:
            记忆 ID
        """
        content = f"任务: {task_description}\n结果: {str(result)[:500]}"
        return self.add_memory(
            content=content,
            metadata={
                "task_description": task_description,
                "success": success,
            },
            memory_type="task",
        )

    def save_user_preference(
        self,
        preference_key: str,
        preference_value: str,
    ) -> str:
        """
        保存用户偏好

        Args:
            preference_key: 偏好键
            preference_value: 偏好值

        Returns:
            记忆 ID
        """
        content = f"用户偏好 - {preference_key}: {preference_value}"
        return self.add_memory(
            content=content,
            metadata={
                "preference_key": preference_key,
                "preference_value": preference_value,
            },
            memory_type="preference",
        )

    def get_stats(self) -> Dict[str, Any]:
        """获取记忆统计信息"""
        count = self._collection.count()
        return {
            "collection_name": self.collection_name,
            "total_memories": count,
            "persist_dir": str(settings.CHROMA_PERSIST_DIR),
        }
