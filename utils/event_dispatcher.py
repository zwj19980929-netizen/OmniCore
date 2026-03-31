"""
EventDispatcher — 统一轮询所有 EventSource 并提交任务。
在 Worker 循环中调用，与现有的 _release_*() 函数并行工作。
"""

from typing import Any, Dict, List, Optional

from utils.logger import log_agent_action, log_warning


_dispatcher_instance: Optional["EventDispatcher"] = None


class EventDispatcher:
    """统一事件分发器。"""

    def __init__(self):
        self._sources: List[Any] = []
        self._initialized = False

    def register_source(self, source: Any) -> None:
        self._sources.append(source)

    def _lazy_init(self) -> None:
        """懒加载：首次 dispatch 时注册所有事件源。"""
        if self._initialized:
            return
        self._initialized = True

        from config.settings import settings

        if not getattr(settings, "EVENT_DRIVEN_ENABLED", True):
            return

        # 注册网页监控
        try:
            from utils.event_sources.web_page_watch import WebPageWatchSource
            self.register_source(WebPageWatchSource(data_dir=str(settings.DATA_DIR)))
        except Exception as e:
            log_warning(f"Failed to register WebPageWatchSource: {e}")

        # 注册邮件监控
        try:
            from utils.event_sources.email_watch import EmailWatchSource
            self.register_source(EmailWatchSource(data_dir=str(settings.DATA_DIR)))
        except Exception as e:
            log_warning(f"Failed to register EmailWatchSource: {e}")

        # 注册 Webhook（仅在 Worker 模式下启动 HTTP 服务器）
        if getattr(settings, "WEBHOOK_ENABLED", False):
            try:
                from utils.event_sources.webhook_source import WebhookSource
                port = getattr(settings, "WEBHOOK_PORT", 9988)
                webhook = WebhookSource(port=port, data_dir=str(settings.DATA_DIR))
                webhook.start()
                self.register_source(webhook)
            except Exception as e:
                log_warning(f"Failed to register WebhookSource: {e}")

    def dispatch_pending_events(self, limit_per_source: int = 5) -> List[str]:
        """轮询所有事件源，为每个事件创建任务，返回已提交的 job_id 列表。"""
        self._lazy_init()

        if not self._sources:
            return []

        from core.runtime import submit_task

        job_ids: List[str] = []
        for source in self._sources:
            try:
                events = source.poll_events(limit=limit_per_source)
                for event in events:
                    result = submit_task(
                        user_input=event.user_input_template,
                        session_id=event.session_id or None,
                        trigger_source=f"event_{event.source_type}",
                        goal_id=event.goal_id or None,
                        project_id=event.project_id or None,
                        todo_id=event.todo_id or None,
                    )
                    job_id = result.get("job_id", "")
                    if job_id:
                        job_ids.append(job_id)
                        log_agent_action(
                            "EventDispatcher",
                            f"[{event.source_type}] event -> job {job_id}",
                        )
            except Exception as e:
                source_type = "unknown"
                try:
                    source_type = source.get_source_type()
                except Exception:
                    pass
                log_warning(f"EventSource {source_type} dispatch failed: {e}")

        return job_ids


def get_event_dispatcher() -> EventDispatcher:
    """获取全局 EventDispatcher 单例。"""
    global _dispatcher_instance
    if _dispatcher_instance is None:
        _dispatcher_instance = EventDispatcher()
    return _dispatcher_instance
