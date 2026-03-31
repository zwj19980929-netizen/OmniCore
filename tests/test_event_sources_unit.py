"""
P3-1 事件驱动信息流 — 单元测试
"""

import hashlib
import json
import os
import queue
import shutil
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_data_dir():
    d = tempfile.mkdtemp(prefix="omnicore_test_events_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ===========================================================================
# 1. WatchEvent / EventSource Protocol
# ===========================================================================

class TestWatchEvent:
    def test_create_event(self):
        from utils.event_sources.base import WatchEvent
        e = WatchEvent(
            event_id="e1",
            source_type="web_page",
            source_id="w1",
            session_id="s1",
            trigger_data={"url": "https://example.com"},
            user_input_template="test",
        )
        assert e.event_id == "e1"
        assert e.source_type == "web_page"
        assert e.goal_id == ""

    def test_event_defaults(self):
        from utils.event_sources.base import WatchEvent
        e = WatchEvent(
            event_id="e2", source_type="email", source_id="w2",
            session_id="", trigger_data={}, user_input_template="t",
        )
        assert e.created_at == ""
        assert e.project_id == ""


# ===========================================================================
# 2. WebPageWatchSource
# ===========================================================================

class TestWebPageWatchSource:
    def test_create_watch(self, tmp_data_dir):
        from utils.event_sources.web_page_watch import WebPageWatchSource
        src = WebPageWatchSource(data_dir=tmp_data_dir)
        wid = src.create_watch({"url": "https://example.com", "session_id": "s1"})
        assert wid.startswith("web_watch_")

    def test_list_watches(self, tmp_data_dir):
        from utils.event_sources.web_page_watch import WebPageWatchSource
        src = WebPageWatchSource(data_dir=tmp_data_dir)
        src.create_watch({"url": "https://a.com"})
        src.create_watch({"url": "https://b.com"})
        assert len(src.list_watches()) == 2

    def test_delete_watch(self, tmp_data_dir):
        from utils.event_sources.web_page_watch import WebPageWatchSource
        src = WebPageWatchSource(data_dir=tmp_data_dir)
        wid = src.create_watch({"url": "https://a.com"})
        src.delete_watch(wid)
        assert len(src.list_watches()) == 0

    def test_pause_resume(self, tmp_data_dir):
        from utils.event_sources.web_page_watch import WebPageWatchSource
        src = WebPageWatchSource(data_dir=tmp_data_dir)
        wid = src.create_watch({"url": "https://a.com"})
        src.pause_watch(wid)
        watches = src.list_watches()
        assert watches[0]["status"] == "paused"
        src.resume_watch(wid)
        watches = src.list_watches()
        assert watches[0]["status"] == "active"

    def test_first_poll_no_event(self, tmp_data_dir):
        """首次检查只记录快照，不触发事件。"""
        from utils.event_sources.web_page_watch import WebPageWatchSource
        src = WebPageWatchSource(data_dir=tmp_data_dir)
        src.create_watch({"url": "https://example.com"})

        html = "<html><body>Hello World</body></html>"
        with patch("utils.event_sources.web_page_watch.WebPageWatchSource._check_page") as mock_check:
            text = "Hello World"
            h = hashlib.md5(text.encode()).hexdigest()
            mock_check.return_value = (False, h, text)
            events = src.poll_events()
            assert len(events) == 0

    def test_poll_detects_change(self, tmp_data_dir):
        """内容变化超过阈值时触发事件。"""
        from utils.event_sources.web_page_watch import WebPageWatchSource
        src = WebPageWatchSource(data_dir=tmp_data_dir)
        src.create_watch({"url": "https://example.com", "change_threshold": 0.05})

        with patch("utils.event_sources.web_page_watch.WebPageWatchSource._check_page") as mock_check:
            mock_check.return_value = (True, "newhash", "new content")
            events = src.poll_events()
            assert len(events) == 1
            assert events[0].source_type == "web_page"
            assert "example.com" in events[0].trigger_data["url"]

    def test_poll_skips_paused(self, tmp_data_dir):
        from utils.event_sources.web_page_watch import WebPageWatchSource
        src = WebPageWatchSource(data_dir=tmp_data_dir)
        wid = src.create_watch({"url": "https://a.com"})
        src.pause_watch(wid)

        with patch("utils.event_sources.web_page_watch.WebPageWatchSource._check_page") as mock_check:
            mock_check.return_value = (True, "h", "t")
            events = src.poll_events()
            assert len(events) == 0

    def test_poll_respects_interval(self, tmp_data_dir):
        """未到检查时间的 watch 不执行。"""
        from utils.event_sources.web_page_watch import WebPageWatchSource
        src = WebPageWatchSource(data_dir=tmp_data_dir)
        src.create_watch({
            "url": "https://a.com",
            "check_interval_seconds": 999999,
        })
        # 手动设置 next_check_at 为未来
        watches = src._load_watches()
        watches[0]["next_check_at"] = (datetime.now() + timedelta(hours=1)).isoformat()
        watches[0]["last_snapshot_hash"] = "oldhash"
        src._save_watches(watches)

        with patch("utils.event_sources.web_page_watch.WebPageWatchSource._check_page") as mock_check:
            mock_check.return_value = (True, "h", "t")
            events = src.poll_events()
            assert len(events) == 0
            mock_check.assert_not_called()

    def test_min_interval_enforced(self, tmp_data_dir):
        """检查间隔不能低于 300 秒。"""
        from utils.event_sources.web_page_watch import WebPageWatchSource
        src = WebPageWatchSource(data_dir=tmp_data_dir)
        src.create_watch({"url": "https://a.com", "check_interval_seconds": 10})
        watches = src._load_watches()
        assert watches[0]["check_interval_seconds"] >= 300

    def test_extract_text_with_selector(self, tmp_data_dir):
        from utils.event_sources.web_page_watch import WebPageWatchSource
        src = WebPageWatchSource(data_dir=tmp_data_dir)
        html = '<html><body><div id="price">$99</div><div>noise</div></body></html>'
        try:
            text = src._extract_text(html, "#price")
            assert "$99" in text
        except ImportError:
            pytest.skip("beautifulsoup4 not installed")

    def test_extract_text_without_bs4(self, tmp_data_dir):
        """没有 bs4 时用 regex 兜底。"""
        from utils.event_sources.web_page_watch import WebPageWatchSource
        src = WebPageWatchSource(data_dir=tmp_data_dir)
        with patch.dict("sys.modules", {"bs4": None}):
            # 重新导入会失败，但 _extract_text 内有 try/except
            text = src._extract_text("<p>hello</p>", "")
            # 用 bs4 或 regex 都应包含 hello
            assert "hello" in text

    def test_compute_change_ratio_no_snapshot(self, tmp_data_dir):
        from utils.event_sources.web_page_watch import WebPageWatchSource
        src = WebPageWatchSource(data_dir=tmp_data_dir)
        ratio = src._compute_change_ratio("nonexistent", "new text")
        assert ratio == 1.0

    def test_compute_change_ratio_with_snapshot(self, tmp_data_dir):
        from utils.event_sources.web_page_watch import WebPageWatchSource
        src = WebPageWatchSource(data_dir=tmp_data_dir)
        # 手动写入快照
        os.makedirs(src._snapshot_dir, exist_ok=True)
        snapshot_path = os.path.join(src._snapshot_dir, "test_w.txt")
        with open(snapshot_path, "w") as f:
            f.write("hello world foo bar")
        ratio = src._compute_change_ratio("test_w", "hello world foo bar")
        assert ratio == 0.0

    def test_snapshot_saved_after_poll(self, tmp_data_dir):
        from utils.event_sources.web_page_watch import WebPageWatchSource
        src = WebPageWatchSource(data_dir=tmp_data_dir)
        wid = src.create_watch({"url": "https://a.com"})

        with patch("utils.event_sources.web_page_watch.WebPageWatchSource._check_page") as mock_check:
            mock_check.return_value = (False, "hash1", "snapshot content")
            src.poll_events()

        snapshot = os.path.join(src._snapshot_dir, f"{wid}.txt")
        assert os.path.exists(snapshot)

    def test_get_source_type(self, tmp_data_dir):
        from utils.event_sources.web_page_watch import WebPageWatchSource
        assert WebPageWatchSource(data_dir=tmp_data_dir).get_source_type() == "web_page"

    def test_poll_limit(self, tmp_data_dir):
        from utils.event_sources.web_page_watch import WebPageWatchSource
        src = WebPageWatchSource(data_dir=tmp_data_dir)
        for i in range(5):
            src.create_watch({"url": f"https://site{i}.com"})

        with patch("utils.event_sources.web_page_watch.WebPageWatchSource._check_page") as mock_check:
            mock_check.return_value = (True, "h", "t")
            events = src.poll_events(limit=2)
            assert len(events) <= 2


# ===========================================================================
# 3. WebhookSource
# ===========================================================================

class TestWebhookSource:
    def test_create_watch(self, tmp_data_dir):
        from utils.event_sources.webhook_source import WebhookSource
        src = WebhookSource(port=0, data_dir=tmp_data_dir)
        wid = src.create_watch({"session_id": "s1"})
        assert wid.startswith("webhook_")
        assert len(src.list_watches()) == 1

    def test_poll_empty(self, tmp_data_dir):
        from utils.event_sources.webhook_source import WebhookSource
        src = WebhookSource(port=0, data_dir=tmp_data_dir)
        assert src.poll_events() == []

    def test_poll_with_queued_event(self, tmp_data_dir):
        from utils.event_sources import webhook_source
        from utils.event_sources.webhook_source import WebhookSource

        src = WebhookSource(port=0, data_dir=tmp_data_dir)
        wid = src.create_watch({"session_id": "s1", "user_input_template": "got event"})

        webhook_source._event_queue.put({"watch_id": wid, "payload": {"key": "val"}})
        events = src.poll_events()
        assert len(events) == 1
        assert events[0].source_type == "webhook"

    def test_pause_skips_events(self, tmp_data_dir):
        from utils.event_sources import webhook_source
        from utils.event_sources.webhook_source import WebhookSource

        src = WebhookSource(port=0, data_dir=tmp_data_dir)
        wid = src.create_watch({"session_id": "s1"})
        src.pause_watch(wid)

        webhook_source._event_queue.put({"watch_id": wid, "payload": {}})
        events = src.poll_events()
        assert len(events) == 0

    def test_delete_watch(self, tmp_data_dir):
        from utils.event_sources.webhook_source import WebhookSource
        src = WebhookSource(port=0, data_dir=tmp_data_dir)
        wid = src.create_watch({})
        src.delete_watch(wid)
        assert len(src.list_watches()) == 0

    def test_get_source_type(self, tmp_data_dir):
        from utils.event_sources.webhook_source import WebhookSource
        assert WebhookSource(data_dir=tmp_data_dir).get_source_type() == "webhook"

    def test_persistence(self, tmp_data_dir):
        from utils.event_sources.webhook_source import WebhookSource
        src = WebhookSource(port=0, data_dir=tmp_data_dir)
        wid = src.create_watch({"note": "test"})
        # 新实例应加载已持久化的 watches
        src2 = WebhookSource(port=0, data_dir=tmp_data_dir)
        assert len(src2.list_watches()) == 1


# ===========================================================================
# 4. EmailWatchSource
# ===========================================================================

class TestEmailWatchSource:
    def test_create_watch(self, tmp_data_dir):
        from utils.event_sources.email_watch import EmailWatchSource
        src = EmailWatchSource(data_dir=tmp_data_dir)
        wid = src.create_watch({
            "imap_host": "imap.example.com",
            "username": "user",
            "password": "${EMAIL_PASSWORD}",
            "session_id": "s1",
        })
        assert wid.startswith("email_watch_")
        assert len(src.list_watches()) == 1

    def test_min_interval(self, tmp_data_dir):
        from utils.event_sources.email_watch import EmailWatchSource
        src = EmailWatchSource(data_dir=tmp_data_dir)
        src.create_watch({
            "imap_host": "imap.example.com",
            "username": "u", "password": "p",
            "check_interval_seconds": 10,
        })
        watches = src._load_watches()
        assert watches[0]["check_interval_seconds"] >= 60

    def test_delete_watch(self, tmp_data_dir):
        from utils.event_sources.email_watch import EmailWatchSource
        src = EmailWatchSource(data_dir=tmp_data_dir)
        wid = src.create_watch({
            "imap_host": "h", "username": "u", "password": "p",
        })
        src.delete_watch(wid)
        assert len(src.list_watches()) == 0

    def test_pause_resume(self, tmp_data_dir):
        from utils.event_sources.email_watch import EmailWatchSource
        src = EmailWatchSource(data_dir=tmp_data_dir)
        wid = src.create_watch({
            "imap_host": "h", "username": "u", "password": "p",
        })
        src.pause_watch(wid)
        assert src.list_watches()[0]["status"] == "paused"
        src.resume_watch(wid)
        assert src.list_watches()[0]["status"] == "active"

    def test_env_ref_resolve(self):
        from utils.event_sources.email_watch import _resolve_env
        with patch.dict(os.environ, {"MY_PASS": "secret123"}):
            assert _resolve_env("${MY_PASS}") == "secret123"
        assert _resolve_env("plaintext") == "plaintext"

    def test_get_source_type(self, tmp_data_dir):
        from utils.event_sources.email_watch import EmailWatchSource
        assert EmailWatchSource(data_dir=tmp_data_dir).get_source_type() == "email"

    def test_poll_skips_future_check(self, tmp_data_dir):
        """未到检查时间的不执行。"""
        from utils.event_sources.email_watch import EmailWatchSource
        src = EmailWatchSource(data_dir=tmp_data_dir)
        src.create_watch({
            "imap_host": "h", "username": "u", "password": "p",
            "check_interval_seconds": 999999,
        })
        watches = src._load_watches()
        watches[0]["next_check_at"] = (datetime.now() + timedelta(hours=2)).isoformat()
        src._save_watches(watches)
        events = src.poll_events()
        assert len(events) == 0


# ===========================================================================
# 5. EventDispatcher
# ===========================================================================

class TestEventDispatcher:
    def test_register_and_dispatch(self):
        from utils.event_dispatcher import EventDispatcher
        from utils.event_sources.base import WatchEvent

        mock_source = MagicMock()
        mock_source.get_source_type.return_value = "test"
        mock_source.poll_events.return_value = [
            WatchEvent(
                event_id="e1", source_type="test", source_id="w1",
                session_id="s1", trigger_data={}, user_input_template="do something",
            )
        ]

        dispatcher = EventDispatcher()
        dispatcher._initialized = True  # skip lazy init
        dispatcher.register_source(mock_source)

        with patch("core.runtime.submit_task") as mock_submit:
            mock_submit.return_value = {"job_id": "j1"}
            job_ids = dispatcher.dispatch_pending_events()
            assert job_ids == ["j1"]
            mock_submit.assert_called_once()
            call_kwargs = mock_submit.call_args
            assert call_kwargs[1]["trigger_source"] == "event_test"

    def test_dispatch_handles_source_error(self):
        from utils.event_dispatcher import EventDispatcher

        mock_source = MagicMock()
        mock_source.get_source_type.return_value = "bad"
        mock_source.poll_events.side_effect = Exception("connection error")

        dispatcher = EventDispatcher()
        dispatcher._initialized = True
        dispatcher.register_source(mock_source)

        # 不应抛出异常
        job_ids = dispatcher.dispatch_pending_events()
        assert job_ids == []

    def test_dispatch_no_sources(self):
        from utils.event_dispatcher import EventDispatcher
        dispatcher = EventDispatcher()
        dispatcher._initialized = True
        assert dispatcher.dispatch_pending_events() == []

    def test_singleton(self):
        import utils.event_dispatcher as mod
        mod._dispatcher_instance = None
        d1 = mod.get_event_dispatcher()
        d2 = mod.get_event_dispatcher()
        assert d1 is d2
        mod._dispatcher_instance = None  # cleanup

    def test_dispatch_multiple_events(self):
        from utils.event_dispatcher import EventDispatcher
        from utils.event_sources.base import WatchEvent

        mock_source = MagicMock()
        mock_source.get_source_type.return_value = "test"
        mock_source.poll_events.return_value = [
            WatchEvent(
                event_id=f"e{i}", source_type="test", source_id="w1",
                session_id="s1", trigger_data={}, user_input_template=f"task {i}",
            )
            for i in range(3)
        ]

        dispatcher = EventDispatcher()
        dispatcher._initialized = True
        dispatcher.register_source(mock_source)

        with patch("core.runtime.submit_task") as mock_submit:
            mock_submit.return_value = {"job_id": "j"}
            job_ids = dispatcher.dispatch_pending_events()
            assert len(job_ids) == 3


# ===========================================================================
# 6. Settings 配置项
# ===========================================================================

class TestEventDrivenSettings:
    def test_defaults(self):
        from config.settings import Settings
        assert hasattr(Settings, "EVENT_DRIVEN_ENABLED")
        assert hasattr(Settings, "WEB_WATCH_MIN_INTERVAL")
        assert hasattr(Settings, "WEB_WATCH_DEFAULT_INTERVAL")
        assert hasattr(Settings, "WEB_WATCH_DEFAULT_THRESHOLD")
        assert hasattr(Settings, "WEBHOOK_ENABLED")
        assert hasattr(Settings, "WEBHOOK_PORT")

    def test_default_values(self):
        from config.settings import Settings
        assert Settings.EVENT_DRIVEN_ENABLED is True
        assert Settings.WEB_WATCH_MIN_INTERVAL == 300
        assert Settings.WEB_WATCH_DEFAULT_INTERVAL == 3600
        assert Settings.WEBHOOK_ENABLED is False
        assert Settings.WEBHOOK_PORT == 9988


# ===========================================================================
# 7. CLI /watch 命令
# ===========================================================================

class TestWatchCommand:
    def _mock_settings(self, tmp_data_dir):
        mock = MagicMock()
        mock.DATA_DIR = tmp_data_dir
        mock.WEB_WATCH_DEFAULT_INTERVAL = 3600
        mock.WEB_WATCH_DEFAULT_THRESHOLD = 0.1
        return mock

    def test_watch_url(self, tmp_data_dir):
        from main import _handle_watch_command
        with patch("config.settings.settings", self._mock_settings(tmp_data_dir)):
            result = _handle_watch_command("/watch url https://example.com", session_id="s1")
            assert result["success"]
            assert "web_watch_" in result["output"]

    def test_watch_list_empty(self, tmp_data_dir):
        from main import _handle_watch_command
        with patch("config.settings.settings", self._mock_settings(tmp_data_dir)):
            result = _handle_watch_command("/watch list")
            assert result["success"]
            assert "暂无" in result["output"]

    def test_watch_stop(self, tmp_data_dir):
        from main import _handle_watch_command
        mock_s = self._mock_settings(tmp_data_dir)
        with patch("config.settings.settings", mock_s):
            result = _handle_watch_command("/watch url https://a.com", session_id="s1")
            wid = [w for w in result["output"].split() if w.startswith("web_watch_")][0]
            stop_result = _handle_watch_command(f"/watch stop {wid}")
            assert stop_result["success"]

    def test_watch_invalid_usage(self, tmp_data_dir):
        from main import _handle_watch_command
        result = _handle_watch_command("/watch badcmd")
        assert not result["success"]
        assert "用法" in result["output"]
