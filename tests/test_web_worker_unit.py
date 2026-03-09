import asyncio

from agents.web_worker import WebWorker


class FakeResponse:
    def __init__(self, text: str, content_type: str = "text/html; charset=utf-8"):
        self.text = text
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        return None


class _FailIfCalledLLM:
    def chat_with_system(self, **_kwargs):
        raise AssertionError("LLM should not be called for deterministic weather URL routing")


def test_extract_static_text_blocks_prefers_long_content():
    worker = WebWorker()
    html = """
    <html>
      <body>
        <article>
          <p>This is a long article paragraph with enough content to be captured by static text extraction.</p>
          <p>This is another detailed paragraph that should also be available for summary-style tasks.</p>
        </article>
      </body>
    </html>
    """

    blocks = worker._extract_static_text_blocks(html, limit=3)

    assert len(blocks) >= 2
    assert "paragraph" in blocks[0]["text"].lower()


def test_prefers_static_text_for_summary_tasks():
    worker = WebWorker()

    assert worker._prefers_static_text("总结这篇文章内容") is True
    assert worker._prefers_static_text("合肥天气预报") is True
    assert worker._prefers_static_text("collect latest links") is False


def test_static_fetch_returns_text_mode_when_summary_task(monkeypatch):
    worker = WebWorker()
    html = """
    <html>
      <body>
        <main>
          <p>This is a long summary paragraph with enough information to be returned directly.</p>
          <p>This second paragraph provides additional useful context for the summary task.</p>
        </main>
      </body>
    </html>
    """

    def _fake_get(*_args, **_kwargs):
        return FakeResponse(html)

    monkeypatch.setattr("agents.web_worker.requests.get", _fake_get)

    result = worker._static_fetch("https://example.com/article", "summary this article", limit=5)

    assert result["success"] is True
    assert result["mode"] == "static_fetch_text"
    assert "text" in result["data"][0]


def test_extract_static_text_blocks_keeps_short_weather_items():
    worker = WebWorker()
    html = """
    <html>
      <body>
        <ul>
          <li>Today Cloudy 8°C humidity 65%</li>
          <li>Tomorrow Sunny 10°C wind 3-4</li>
        </ul>
      </body>
    </html>
    """

    blocks = worker._extract_static_text_blocks(html, limit=5, task_description="Hefei weather forecast")

    assert len(blocks) == 2
    assert "Cloudy" in blocks[0]["text"]


def test_static_fetch_rejects_link_only_weather_payload(monkeypatch):
    worker = WebWorker()
    html = """
    <html>
      <body>
        <a href="/weather/101220101.shtml">合肥天气</a>
        <a href="/weather/101220501.shtml">马鞍山天气</a>
        <a href="/alarm/">天气预警</a>
      </body>
    </html>
    """

    def _fake_get(*_args, **_kwargs):
        return FakeResponse(html)

    monkeypatch.setattr("agents.web_worker.requests.get", _fake_get)

    result = worker._static_fetch("https://www.weather.com.cn/weather/101220101.shtml", "合肥天气预报", limit=5)

    assert result["success"] is False
    assert "usable detail data" in result["error"]


def test_determine_target_url_uses_direct_weather_url_without_llm():
    worker = WebWorker(llm_client=_FailIfCalledLLM())

    result = asyncio.run(
        worker.determine_target_url(
            "Use https://www.weather.com.cn/weather/101220101.shtml to read Hefei weather after rendering"
        )
    )

    assert result["url"] == "https://www.weather.com.cn/weather/101220101.shtml"
    assert result["need_search"] is False
    assert result["search_query"] == ""


def test_determine_target_url_builds_site_constrained_weather_query_without_llm():
    worker = WebWorker(llm_client=_FailIfCalledLLM())

    result = asyncio.run(
        worker.determine_target_url(
            "Directly obtain 合肥的明天（2026-03-07）天气 from weather.com.cn as the primary weather source."
        )
    )

    assert result["url"] == ""
    assert result["need_search"] is True
    assert result["search_query"] == "site:weather.com.cn 合肥 明天 天气"
