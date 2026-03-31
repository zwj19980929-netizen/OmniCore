"""
WebhookSource — HTTP Webhook 事件接收。
启动一个轻量 HTTP 服务器监听 webhook 调用。
"""

import json
import queue
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List

from utils.event_sources.base import WatchEvent
from utils.logger import log_agent_action, log_warning


_event_queue: queue.Queue = queue.Queue()


class _WebhookHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器，接收 POST /webhook/{watch_id}。"""

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""
        try:
            payload = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            payload = {"raw": body.decode(errors="replace")}

        # 从路径解析 watch_id: /webhook/{watch_id}
        path = self.path.strip("/")
        parts = path.split("/", 1)
        watch_id = parts[1] if len(parts) > 1 else parts[0]

        _event_queue.put({"watch_id": watch_id, "payload": payload})

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, format, *args):
        pass  # 静默 HTTP 日志


class WebhookSource:
    """Webhook 事件源。"""

    def __init__(self, port: int = 9988, data_dir: str = "data"):
        self.port = port
        self._server: HTTPServer = None
        self._thread: threading.Thread = None
        self._watches_file = f"{data_dir}/webhook_watches.json"
        self._watches: Dict[str, Dict] = self._load_watches_from_disk()

    def get_source_type(self) -> str:
        return "webhook"

    def start(self) -> None:
        """启动 HTTP 服务器监听。"""
        if self._server is not None:
            return
        try:
            self._server = HTTPServer(("0.0.0.0", self.port), _WebhookHandler)
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True
            )
            self._thread.start()
            log_agent_action("WebhookSource", f"Listening on port {self.port}")
        except OSError as e:
            log_warning(f"WebhookSource failed to start on port {self.port}: {e}")

    def stop(self) -> None:
        """停止 HTTP 服务器。"""
        if self._server:
            self._server.shutdown()
            self._server = None

    def create_watch(self, config: Dict[str, Any]) -> str:
        watch_id = f"webhook_{uuid.uuid4().hex[:12]}"
        self._watches[watch_id] = {
            "watch_id": watch_id,
            "session_id": config.get("session_id", ""),
            "user_input_template": config.get(
                "user_input_template",
                "收到 Webhook 事件，请处理。",
            ),
            "note": config.get("note", ""),
            "status": "active",
        }
        self._persist_watches()
        log_agent_action("WebhookSource", f"Created webhook watch: {watch_id}")
        return watch_id

    def poll_events(self, limit: int = 10) -> List[WatchEvent]:
        events: List[WatchEvent] = []
        while len(events) < limit:
            try:
                item = _event_queue.get_nowait()
            except queue.Empty:
                break

            watch_id = item.get("watch_id", "")
            watch_config = self._watches.get(watch_id, {})

            if watch_config.get("status") == "paused":
                continue

            payload = item.get("payload", {})
            template = watch_config.get(
                "user_input_template",
                f"收到 Webhook 事件，内容: {str(payload)[:200]}",
            )

            events.append(WatchEvent(
                event_id=f"wh_{uuid.uuid4().hex[:8]}",
                source_type="webhook",
                source_id=watch_id,
                session_id=watch_config.get("session_id", ""),
                trigger_data=payload,
                user_input_template=template,
            ))

        return events

    def list_watches(self) -> List[Dict[str, Any]]:
        return [{"watch_id": k, **v} for k, v in self._watches.items()
                if v.get("status") != "deleted"]

    def pause_watch(self, watch_id: str) -> None:
        if watch_id in self._watches:
            self._watches[watch_id]["status"] = "paused"
            self._persist_watches()

    def resume_watch(self, watch_id: str) -> None:
        if watch_id in self._watches:
            self._watches[watch_id]["status"] = "active"
            self._persist_watches()

    def delete_watch(self, watch_id: str) -> None:
        self._watches.pop(watch_id, None)
        self._persist_watches()

    def _persist_watches(self) -> None:
        import os
        os.makedirs(os.path.dirname(self._watches_file) or ".", exist_ok=True)
        try:
            with open(self._watches_file, "w", encoding="utf-8") as f:
                json.dump(self._watches, f, ensure_ascii=False, indent=2)
        except OSError as e:
            log_warning(f"Failed to persist webhook watches: {e}")

    def _load_watches_from_disk(self) -> Dict[str, Dict]:
        import os
        if not os.path.exists(self._watches_file):
            return {}
        try:
            with open(self._watches_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
