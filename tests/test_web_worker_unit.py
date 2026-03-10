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


def test_smart_scrape_prefers_search_candidates_over_section_urls(monkeypatch):
    worker = WebWorker()
    attempted_urls = []

    async def _fake_determine_target_url(_task_description):
        return {
            "url": "https://www.reuters.com/world/middle-east/",
            "backup_urls": [],
            "need_search": True,
            "search_query": "US Iran strikes Reuters",
        }

    async def _fake_gather_search_candidates(*_args, **_kwargs):
        return {
            "queries": ["US Iran strikes Reuters"],
            "cards": [
                {
                    "title": "US strikes in Iran raise tensions",
                    "link": "https://www.reuters.com/world/article-123",
                    "source": "Reuters",
                    "snippet": "Direct military action was reported.",
                }
            ],
            "urls": ["https://www.reuters.com/world/article-123"],
            "serp_sufficient": False,
        }

    def _fake_static_fetch(url, _task_description, _limit):
        attempted_urls.append(url)
        if url.endswith("article-123"):
            return {
                "success": True,
                "data": [{"title": "US strikes in Iran raise tensions"}],
                "count": 1,
                "source": url,
                "mode": "static_fetch",
            }
        return {"success": False, "error": "section page", "data": [], "url": url}

    monkeypatch.setattr(worker, "determine_target_url", _fake_determine_target_url)
    monkeypatch.setattr(worker, "gather_search_candidates", _fake_gather_search_candidates)
    monkeypatch.setattr(worker, "_static_fetch", _fake_static_fetch)

    result = asyncio.run(worker.smart_scrape("", "核实近期美国与伊朗之间军事行动", limit=3))

    assert result["success"] is True
    assert attempted_urls[0] == "https://www.reuters.com/world/article-123"
