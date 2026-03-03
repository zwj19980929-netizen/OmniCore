import asyncio
import time

from agents.web_worker import WebWorker
from config.settings import settings
from core.llm_cache import LLMCache
from utils.browser_toolkit import ToolkitResult


class FakeLLM:
    def __init__(self, responses):
        self.model = "fake-model"
        self._responses = list(responses)
        self.calls = 0

    def chat_with_system(self, **_kwargs):
        response = self._responses[self.calls]
        self.calls += 1
        return response

    def parse_json_response(self, response):
        return response


class SlowFakeLLM(FakeLLM):
    def __init__(self, responses, delay_seconds: float = 0.05):
        super().__init__(responses)
        self.delay_seconds = delay_seconds

    def chat_with_system(self, **_kwargs):
        time.sleep(self.delay_seconds)
        return super().chat_with_system(**_kwargs)


class FakeToolkit:
    def __init__(self, url: str, html: str):
        self._url = url
        self._html = html

    async def get_current_url(self):
        return ToolkitResult(success=True, data=self._url)

    async def get_page_html(self):
        return ToolkitResult(success=True, data=self._html)


def test_determine_target_url_uses_cache_on_repeated_task(monkeypatch):
    monkeypatch.setattr(settings, "LLM_CACHE_ENABLED", True)
    llm = FakeLLM([
        {"url": "https://example.com/tasks", "backup_urls": [], "need_search": False}
    ])
    worker = WebWorker(llm_client=llm)
    worker.cache = LLMCache(max_entries=8)

    first = asyncio.run(worker.determine_target_url("find the example task page"))
    second = asyncio.run(worker.determine_target_url("find the example task page"))

    assert first == second
    assert llm.calls == 1
    assert worker.cache.snapshot_stats()["hits"] == 1


def test_analyze_page_structure_uses_cache_for_same_page_fingerprint(monkeypatch):
    monkeypatch.setattr(settings, "LLM_CACHE_ENABLED", True)
    llm = FakeLLM([
        {
            "success": True,
            "item_selector": "li.task-row",
            "fields": {"title": "a.title"},
        }
    ])
    worker = WebWorker(llm_client=llm)
    worker.cache = LLMCache(max_entries=8)
    toolkit = FakeToolkit(
        "https://example.com/tasks",
        "<html><body><ul><li class='task-row'><a class='title'>Task A</a></li></ul></body></html>",
    )

    first = asyncio.run(worker.analyze_page_structure(toolkit, "collect task titles"))
    second = asyncio.run(worker.analyze_page_structure(toolkit, "collect task titles"))

    assert first == second
    assert first["item_selector"] == "li.task-row"
    assert llm.calls == 1
    assert worker.cache.snapshot_stats()["hits"] == 1


def test_determine_target_url_singleflight_deduplicates_inflight_requests(monkeypatch):
    async def _run():
        monkeypatch.setattr(settings, "LLM_CACHE_ENABLED", True)
        monkeypatch.setattr(settings, "LLM_CACHE_INFLIGHT_WAIT_SECONDS", 1)
        llm = SlowFakeLLM([
            {"url": "https://example.com/tasks", "backup_urls": [], "need_search": False}
        ])
        worker = WebWorker(llm_client=llm)
        worker.cache = LLMCache(max_entries=8)

        first, second = await asyncio.gather(
            worker.determine_target_url("find the example task page"),
            worker.determine_target_url("find the example task page"),
        )

        assert first == second
        assert llm.calls == 1
        assert worker.cache.snapshot_stats()["singleflight_waits"] == 1

    asyncio.run(_run())
