"""
EventSource 协议 — 所有事件源的统一接口。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Protocol


@dataclass
class WatchEvent:
    """统一的事件格式。"""
    event_id: str
    source_type: str                        # "web_page" | "email" | "webhook" | "directory" | "schedule"
    source_id: str                          # Watch 配置的 ID
    session_id: str
    trigger_data: Dict[str, Any]            # 触发事件的原始数据
    user_input_template: str                # 任务描述模板，支持 {变量} 替换
    goal_id: str = ""
    project_id: str = ""
    todo_id: str = ""
    created_at: str = ""


class EventSource(Protocol):
    """事件源协议，所有实现必须满足此接口。"""

    def get_source_type(self) -> str:
        """返回事件源类型标识。"""
        ...

    def poll_events(self, limit: int = 10) -> List[WatchEvent]:
        """
        轮询并返回新事件列表。
        实现应保证幂等：同一事件不能返回两次。
        """
        ...

    def create_watch(self, config: Dict[str, Any]) -> str:
        """创建监控配置，返回 watch_id。"""
        ...

    def pause_watch(self, watch_id: str) -> None:
        """暂停监控。"""
        ...

    def resume_watch(self, watch_id: str) -> None:
        """恢复监控。"""
        ...

    def delete_watch(self, watch_id: str) -> None:
        """删除监控。"""
        ...

    def list_watches(self) -> List[Dict[str, Any]]:
        """列出所有监控配置。"""
        ...
