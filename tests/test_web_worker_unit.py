import asyncio

from agents.web_worker import WebWorker
from unittest.mock import patch

from utils.web_result_normalizer import normalize_web_results


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


def test_validate_data_quality_accepts_detail_summary_without_llm():
    worker = WebWorker()

    data = [
        {
            "title": "OpenAI API Updates",
            "summary": "The latest release adds better structured outputs and richer metadata for production workloads.",
        }
    ]

    result = worker.validate_data_quality(data, "总结这篇文章的主要内容", limit=3)

    assert result["valid"] is True
    assert "详情页正文或摘要信号" in result["reason"]


def test_extract_direct_urls_strips_fullwidth_punctuation():
    worker = WebWorker()

    urls = worker._extract_direct_urls("打开 https://news.ycombinator.com），抓取前 3 条新闻")

    assert urls == ["https://news.ycombinator.com"]


def test_extract_data_with_selectors_supports_attribute_style_fields():
    worker = WebWorker()

    class _FakeElement:
        def __init__(self, tag, text="", attrs=None):
            self.tag = tag
            self.text = text
            self.attrs = attrs or {}

        async def query_selector(self, selector):
            if selector == "a":
                return _FakeElement("a", "Story 1", {"href": "https://example.com/1"})
            return None

        async def evaluate(self, _script):
            return self.tag

        async def inner_text(self):
            return self.text

        async def get_attribute(self, name):
            return self.attrs.get(name)

    class _FakeToolkit:
        async def query_all(self, selector):
            assert selector == "tr.athing"
            return type("Result", (), {"success": True, "data": [_FakeElement("tr", attrs={"id": "123"})]})()

    data = asyncio.run(
        worker.extract_data_with_selectors(
            _FakeToolkit(),
            {
                "item_selector": "tr.athing",
                "fields": {
                    "id": "@id",
                    "title": "a",
                    "link": "a/@href",
                },
            },
            limit=1,
        )
    )

    assert data == [
        {
            "index": 1,
            "id": "123",
            "title": "Story 1",
            "link": "https://example.com/1",
        }
    ]


def test_extract_data_with_selectors_joins_relative_url_fields():
    worker = WebWorker()

    class _FakeElement:
        async def query_selector(self, selector):
            if selector == "a":
                return self
            return None

        async def evaluate(self, _script):
            return "a"

        async def inner_text(self):
            return "Story 2"

        async def get_attribute(self, name):
            if name == "href":
                return "/repo/story-2"
            return None

    class _FakeToolkit:
        async def query_all(self, selector):
            assert selector == "article"
            return type("Result", (), {"success": True, "data": [_FakeElement()]})()

        async def get_current_url(self):
            return type("Result", (), {"success": True, "data": "https://example.com/list"})()

    data = asyncio.run(
        worker.extract_data_with_selectors(
            _FakeToolkit(),
            {
                "item_selector": "article",
                "fields": {
                    "title": "a",
                    "url": "a/@href",
                },
            },
            limit=1,
        )
    )

    assert data == [
        {
            "index": 1,
            "title": "Story 2",
            "link": "https://example.com/repo/story-2",
            "url": "https://example.com/repo/story-2",
        }
    ]


def test_extract_field_value_supports_contains_style_selector():
    worker = WebWorker()

    class _FakeCell:
        def __init__(self, text):
            self.text = text

        async def inner_text(self):
            return self.text

        async def evaluate(self, _script):
            return "td"

    class _FakeRow:
        async def query_selector_all(self, selector):
            assert selector == "td"
            return [_FakeCell("发布日期"), _FakeCell("2026-03-13"), _FakeCell("高危")]

        async def query_selector(self, selector):
            raise AssertionError(f"unexpected selector call: {selector}")

    result = asyncio.run(worker._extract_field_value(_FakeRow(), "date", 'td:contains("2026-03-13")'))

    assert result == {"date": "2026-03-13"}


def test_validate_data_quality_accepts_basic_news_list_without_extra_metadata():
    worker = WebWorker(llm_client=_FailIfCalledLLM())

    quality = worker.validate_data_quality(
        [
            {"title": "Story 1 headline", "link": "https://example.com/1"},
            {"title": "Story 2 headline", "link": "https://example.com/2"},
            {"title": "Story 3 headline", "link": "https://example.com/3"},
        ],
        "抓取前 3 条新闻的标题和链接",
        limit=3,
    )

    assert quality["valid"] is True


def test_extract_static_links_prefers_detail_rows_over_same_page_filters():
    worker = WebWorker()
    html = """
    <html><body>
      <a href="/models?pipeline_tag=text-generation" class="rounded-lg">Text Generation</a>
      <a href="/models?library=tf" class="rounded-lg">TensorFlow</a>
      <a href="/org/model-a" class="flex item-row">Org/Model-A Text Generation • Updated 1 day ago</a>
      <a href="/org/model-b" class="flex item-row">Org/Model-B Image-to-Text • Updated 2 days ago</a>
      <a href="/pricing">Pricing</a>
    </body></html>
    """

    data = worker._extract_static_links(
        html,
        "https://huggingface.co/models",
        "去 https://huggingface.co/models 抓取前 2 个模型的名称和链接",
        limit=2,
    )

    assert [item["link"] for item in data] == [
        "https://huggingface.co/org/model-a",
        "https://huggingface.co/org/model-b",
    ]


def test_extract_static_links_scans_deep_enough_to_reach_detail_rows_after_many_filters():
    worker = WebWorker()
    filter_links = "\n".join(
        f'<a href="/models?pipeline_tag=tag-{idx}" class="rounded-lg">Filter {idx}</a>'
        for idx in range(1, 40)
    )
    detail_links = "\n".join(
        f'<a href="/org/model-{idx}" class="flex item-row">Org/Model-{idx} Updated recently with metrics</a>'
        for idx in range(1, 13)
    )
    html = f"<html><body>{filter_links}{detail_links}</body></html>"

    data = worker._extract_static_links(
        html,
        "https://huggingface.co/models",
        "去 https://huggingface.co/models 抓取前 10 个模型的名称和链接",
        limit=10,
    )

    assert len(data) == 10
    assert all("/org/model-" in item["link"] for item in data)


def test_validate_data_quality_rejects_same_page_filter_links_for_list_task():
    worker = WebWorker(llm_client=_FailIfCalledLLM())

    quality = worker.validate_data_quality(
        [
            {"title": "Text Generation", "link": "https://huggingface.co/models?pipeline_tag=text-generation"},
            {"title": "TensorFlow", "link": "https://huggingface.co/models?library=tf"},
            {"title": "Page 2", "link": "https://huggingface.co/models?p=1"},
        ],
        "去 https://huggingface.co/models 抓取前 3 个模型的名称和链接",
        limit=3,
    )

    assert quality["valid"] is False


def test_extract_news_links_fallback_prefers_detail_like_links():
    worker = WebWorker()

    class _FakeToolkit:
        async def evaluate_js(self, _script, _limit):
            return type(
                "Result",
                (),
                {
                    "success": True,
                    "data": [
                        {"title": "Text Generation", "link": "https://huggingface.co/models?pipeline_tag=text-generation"},
                        {"title": "sentence-transformers", "link": "https://huggingface.co/models?library=sentence-transformers"},
                        {"title": "Jackrong/Qwen3.5-27B-Claude-4.6-Opus-Reasoning-Distilled", "link": "https://huggingface.co/Jackrong/Qwen3.5-27B-Claude-4.6-Opus-Reasoning-Distilled"},
                        {"title": "Tesslate/OmniCoder-9B", "link": "https://huggingface.co/Tesslate/OmniCoder-9B"},
                        {"title": "LocoreMind/LocoTrainer-4B", "link": "https://huggingface.co/LocoreMind/LocoTrainer-4B"},
                    ],
                },
            )()

        async def get_current_url(self):
            return type("Result", (), {"success": True, "data": "https://huggingface.co/models"})()

    data = asyncio.run(
        worker.extract_news_links_fallback(
            _FakeToolkit(),
            "去 https://huggingface.co/models 抓取前 3 个模型的名称和链接",
            limit=3,
        )
    )

    assert [item["link"] for item in data] == [
        "https://huggingface.co/Jackrong/Qwen3.5-27B-Claude-4.6-Opus-Reasoning-Distilled",
        "https://huggingface.co/Tesslate/OmniCoder-9B",
        "https://huggingface.co/LocoreMind/LocoTrainer-4B",
    ]


def test_normalize_web_results_filters_noise_and_canonicalizes_url():
    cleaned = normalize_web_results(
        [
            {"index": 1, "title": "Home", "link": "https://example.com/"},
            {
                "index": 2,
                "title": "OpenViking",
                "title_url": "https://github.com/volcengine/OpenViking",
                "summary": "AI agent runtime",
            },
            {
                "index": 3,
                "name": "OpenViking",
                "link": "https://github.com/volcengine/OpenViking",
            },
        ],
        "抓取前 3 个仓库的标题和链接",
        limit=5,
        understanding={"page_type": "list"},
    )

    assert cleaned == [
        {
            "index": 1,
            "title": "OpenViking",
            "url": "https://github.com/volcengine/OpenViking",
            "link": "https://github.com/volcengine/OpenViking",
            "summary": "AI agent runtime",
        }
    ]


def test_normalize_web_results_decodes_search_redirect_url():
    cleaned = normalize_web_results(
        [
            {
                "index": 1,
                "title": "API Platform | OpenAI",
                "link": "https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly9vcGVuYWkuY29tL2FwaS8&ntb=1",
            }
        ],
        "抓取前 1 条结果的标题和链接",
        limit=3,
        understanding={"page_type": "serp"},
    )

    assert cleaned == [
        {
            "index": 1,
            "title": "API Platform | OpenAI",
            "url": "https://openai.com/api/",
            "link": "https://openai.com/api/",
        }
    ]


def test_normalize_web_results_filters_search_vertical_urls_and_prefers_target_url():
    cleaned = normalize_web_results(
        [
            {
                "index": 1,
                "title": "OpenAI API",
                "link": "https://www.bing.com/images/search?view=detailV2&id=abc&mediaurl=https://images.example.com/openai.png&q=openai+api&idpp=rc",
            },
            {
                "index": 2,
                "title": "API Platform | OpenAI",
                "link": "https://www.bing.com/images/search?view=detailV2&id=def&mediaurl=https://images.example.com/cover.png&q=openai+api&idpp=rc",
                "target_url": "https://openai.com/api/",
            },
        ],
        "抓取前 3 条结果的标题和链接",
        limit=5,
        understanding={"page_type": "serp"},
    )

    assert cleaned == [
        {
            "index": 1,
            "title": "API Platform | OpenAI",
            "url": "https://openai.com/api/",
            "link": "https://openai.com/api/",
        }
    ]


def test_normalize_web_results_filters_bing_copilotsearch_intermediary_url():
    cleaned = normalize_web_results(
        [
            {
                "index": 1,
                "title": "API Platform | OpenAI",
                "link": "https://www.bing.com/ck/a?!&&p=83057eb2981e834e33bd60a87ae263678083f3e69416b37470107d75ce8e981a&u=a1L2NvcGlsb3RzZWFyY2g_cT1vcGVuYWkrYXBpJmZvcm09Q1NCUkFORA&ntb=1",
            },
            {
                "index": 2,
                "title": "Pricing",
                "link": "https://openai.com/api/pricing/",
            },
        ],
        "抓取前 3 条结果的标题和链接",
        limit=5,
        understanding={"page_type": "serp"},
    )

    assert cleaned == [
        {
            "index": 1,
            "title": "Pricing",
            "url": "https://openai.com/api/pricing/",
            "link": "https://openai.com/api/pricing/",
        }
    ]


def test_normalize_web_results_reconstructs_display_style_urls():
    cleaned = normalize_web_results(
        [
            {
                "index": 1,
                "title": "API Platform | OpenAI",
                "url": "https://openai.com › api 翻译此结果",
                "summary": "Build with OpenAI API.",
            }
        ],
        "抓取前 1 条结果的标题和链接",
        limit=3,
        understanding={"page_type": "serp"},
    )

    assert cleaned == [
        {
            "index": 1,
            "title": "API Platform | OpenAI",
            "url": "https://openai.com/api",
            "link": "https://openai.com/api",
            "summary": "Build with OpenAI API.",
        }
    ]


def test_extract_hackernews_static_items_prefers_story_rows():
    worker = WebWorker()
    html = """
    <html><body>
      <table>
        <tr class="athing" id="1">
          <td class="title"><span class="titleline"><a href="https://example.com/story-1">Story 1</a></span></td>
        </tr>
        <tr><td class="subtext">100 points</td></tr>
        <tr class="athing" id="2">
          <td class="title"><span class="titleline"><a href="item?id=2">Story 2</a></span></td>
        </tr>
      </table>
      <a href="/news">Hacker News</a>
    </body></html>
    """

    data = worker._extract_hackernews_static_items(html, "https://news.ycombinator.com/", limit=2)

    assert data == [
        {
            "rank": 1,
            "id": "1",
            "title": "Story 1",
            "link": "https://example.com/story-1",
        },
        {
            "rank": 2,
            "id": "2",
            "title": "Story 2",
            "link": "https://news.ycombinator.com/item?id=2",
        },
    ]


def test_extract_static_next_page_url_prefers_more_link():
    worker = WebWorker()
    html = """
    <html><body>
      <a href="/news">Home</a>
      <a href="news?p=2" class="morelink" rel="next">More</a>
    </body></html>
    """

    next_url = worker._extract_static_next_page_url(html, "https://news.ycombinator.com/")

    assert next_url == "https://news.ycombinator.com/news?p=2"


def test_static_fetch_can_follow_next_page_for_list_tasks():
    worker = WebWorker()
    page_1 = """
    <html><body>
      <table>
        <tr class="athing" id="1"><td class="title"><span class="titleline"><a href="https://example.com/1">Story 1</a></span></td></tr>
        <tr class="athing" id="2"><td class="title"><span class="titleline"><a href="https://example.com/2">Story 2</a></span></td></tr>
      </table>
      <a href="news?p=2" class="morelink" rel="next">More</a>
    </body></html>
    """
    page_2 = """
    <html><body>
      <table>
        <tr class="athing" id="3"><td class="title"><span class="titleline"><a href="https://example.com/3">Story 3</a></span></td></tr>
      </table>
    </body></html>
    """

    class _Response:
        def __init__(self, text):
            self.text = text
            self.headers = {"content-type": "text/html; charset=utf-8"}
            self.encoding = "utf-8"
            self.apparent_encoding = "utf-8"

        def raise_for_status(self):
            return None

    class _Session:
        def __init__(self):
            self.headers = {}
            self.calls = []

        def get(self, url, timeout=15):
            self.calls.append((url, timeout))
            if url.endswith("news?p=2"):
                return _Response(page_2)
            return _Response(page_1)

    with patch("agents.web_worker.requests.Session", return_value=_Session()):
        result = worker._static_fetch(
            "https://news.ycombinator.com/",
            "去 https://news.ycombinator.com/ 抓取前 3 条新闻的标题和链接",
            limit=3,
        )

    assert result["success"] is True
    assert result["count"] == 3
    assert [item["title"] for item in result["data"]] == ["Story 1", "Story 2", "Story 3"]


def test_fallback_search_queries_trust_explicit_base_query_for_weather_tasks():
    worker = WebWorker()

    queries = worker._fallback_search_queries(
        "Obtain 合肥的明天（2026-03-16）天气 from a relevant city weather forecast page.",
        base_query="合肥 明天 天气",
    )

    assert queries == ["合肥 明天 天气"]


def test_smart_scrape_prefers_search_candidates_over_section_urls(monkeypatch):
    worker = WebWorker()
    attempted_urls = []
    closed = {"value": False}

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

    class _FakeToolkit:
        page = None

        async def create_page(self):
            return None

        async def close(self):
            closed["value"] = True

    monkeypatch.setattr(worker, "determine_target_url", _fake_determine_target_url)
    monkeypatch.setattr(worker, "gather_search_candidates", _fake_gather_search_candidates)
    monkeypatch.setattr(worker, "_static_fetch", _fake_static_fetch)
    monkeypatch.setattr(worker, "_create_toolkit", lambda **_kwargs: _FakeToolkit())
    monkeypatch.setattr(worker, "validate_data_quality", lambda *_args, **_kwargs: {"valid": True})

    result = asyncio.run(worker.smart_scrape("", "核实近期美国与伊朗之间军事行动", limit=3))

    assert result["success"] is True
    assert attempted_urls[0] == "https://www.reuters.com/world/article-123"
    assert closed["value"] is True


def test_smart_scrape_reuses_search_page_and_opens_target_in_new_tab(monkeypatch):
    worker = WebWorker()
    opened_tabs = []
    visited_urls = []
    closed = {"value": False}

    async def _fake_determine_target_url(_task_description):
        return {
            "url": "",
            "backup_urls": [],
            "need_search": True,
            "search_query": "hefei weather today",
        }

    async def _fake_gather_search_candidates(*_args, **_kwargs):
        return {
            "queries": ["hefei weather today"],
            "cards": [
                {
                    "title": "Hefei Weather",
                    "link": "https://weather.example.com/hefei",
                    "source": "Example Weather",
                    "snippet": "Today cloudy",
                }
            ],
            "urls": ["https://weather.example.com/hefei"],
            "serp_sufficient": False,
        }

    class _FakeToolkit:
        page = None

        @staticmethod
        def _result(success=True, data=None, error=""):
            return type("Result", (), {"success": success, "data": data, "error": error})()

        def __init__(self):
            self.current_url = "https://www.baidu.com/s?wd=hefei+weather+today"

        async def create_page(self):
            return None

        async def get_current_url(self):
            return self._result(True, self.current_url)

        async def new_tab(self, url=""):
            opened_tabs.append(url)
            self.current_url = url
            return self._result(True, url)

        async def wait_for_load(self, *_args, **_kwargs):
            return self._result(True)

        async def human_delay(self, *_args, **_kwargs):
            return None

        async def goto(self, url, **_kwargs):
            visited_urls.append(url)
            self.current_url = url
            return self._result(True, url)

        async def wait_for_selector(self, *_args, **_kwargs):
            return self._result(True)

        async def detect_captcha(self):
            return self._result(True, {"has_captcha": False})

        async def scroll_down(self, *_args, **_kwargs):
            return self._result(True)

        async def semantic_snapshot(self, *_args, **_kwargs):
            return self._result(True, {"page_type": "detail", "cards": [], "collections": []})

        async def close(self):
            closed["value"] = True

    async def _fake_analyze_page_structure(_tk, _task_description):
        return {"item_selector": "li.weather", "fields": {"text": "text()"}}

    async def _fake_extract_data(_tk, _config, _limit):
        return [
            {"text": "Today cloudy 18C"},
            {"text": "Humidity 60%"},
            {"text": "Wind 3 level"},
        ]

    monkeypatch.setattr(worker, "determine_target_url", _fake_determine_target_url)
    monkeypatch.setattr(worker, "gather_search_candidates", _fake_gather_search_candidates)
    monkeypatch.setattr(worker, "_can_use_static_fetch", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(worker, "_create_toolkit", lambda **_kwargs: _FakeToolkit())
    monkeypatch.setattr(worker, "analyze_page_structure", _fake_analyze_page_structure)
    monkeypatch.setattr(worker, "extract_data_with_selectors", _fake_extract_data)
    monkeypatch.setattr(worker, "validate_data_quality", lambda *_args, **_kwargs: {"valid": True})

    result = asyncio.run(worker.smart_scrape("", "帮我查一下合肥今天天气", limit=3))

    assert result["success"] is True
    assert opened_tabs == ["https://weather.example.com/hefei"]
    assert visited_urls == []
    assert closed["value"] is True


def test_smart_scrape_retries_next_search_candidate_from_same_search_page(monkeypatch):
    worker = WebWorker()
    opened_tabs = []
    closed_tabs = {"count": 0}
    closed = {"value": False}
    gather_calls = {"count": 0}

    async def _fake_determine_target_url(_task_description):
        return {
            "url": "",
            "backup_urls": [],
            "need_search": True,
            "search_query": "hefei weather today",
        }

    async def _fake_gather_search_candidates(*_args, **_kwargs):
        gather_calls["count"] += 1
        return {
            "queries": ["hefei weather today"],
            "cards": [
                {
                    "title": "Bad Weather Page",
                    "link": "https://weather.example.com/bad",
                    "source": "Example Weather",
                    "snippet": "Incomplete page",
                },
                {
                    "title": "Good Weather Page",
                    "link": "https://weather.example.com/good",
                    "source": "Example Weather",
                    "snippet": "Today cloudy",
                },
            ],
            "urls": [
                "https://weather.example.com/bad",
                "https://weather.example.com/good",
            ],
            "serp_sufficient": False,
        }

    class _FakeToolkit:
        page = None

        @staticmethod
        def _result(success=True, data=None, error=""):
            return type("Result", (), {"success": success, "data": data, "error": error})()

        def __init__(self):
            self.search_url = "https://www.baidu.com/s?wd=hefei+weather+today"
            self.current_url = self.search_url

        async def create_page(self):
            return None

        async def get_current_url(self):
            return self._result(True, self.current_url)

        async def new_tab(self, url=""):
            opened_tabs.append(url)
            self.current_url = url
            return self._result(True, url)

        async def close_tab(self):
            closed_tabs["count"] += 1
            self.current_url = self.search_url
            return self._result(True)

        async def wait_for_load(self, *_args, **_kwargs):
            return self._result(True)

        async def human_delay(self, *_args, **_kwargs):
            return None

        async def goto(self, url, **_kwargs):
            self.current_url = url
            return self._result(True, url)

        async def wait_for_selector(self, *_args, **_kwargs):
            return self._result(True)

        async def detect_captcha(self):
            return self._result(True, {"has_captcha": False})

        async def scroll_down(self, *_args, **_kwargs):
            return self._result(True)

        async def semantic_snapshot(self, *_args, **_kwargs):
            page_type = "serp" if "baidu.com/s?" in self.current_url else "detail"
            return self._result(True, {"page_type": page_type, "cards": [], "collections": []})

        async def query_all(self, *_args, **_kwargs):
            return self._result(True, [])

        async def close(self):
            closed["value"] = True

    async def _fake_analyze_page_structure(_tk, _task_description):
        return {"item_selector": "li.weather", "fields": {"text": "text()"}}

    async def _fake_extract_data(tk, _config, _limit):
        if str(getattr(tk, "current_url", "")).endswith("/good"):
            return [
                {"text": "Today cloudy 18C"},
                {"text": "Humidity 60%"},
                {"text": "Wind 3 level"},
            ]
        return []

    async def _empty_async(*_args, **_kwargs):
        return []

    async def _no_navigation(*_args, **_kwargs):
        return None

    monkeypatch.setattr(worker, "determine_target_url", _fake_determine_target_url)
    monkeypatch.setattr(worker, "gather_search_candidates", _fake_gather_search_candidates)
    monkeypatch.setattr(worker, "_can_use_static_fetch", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(worker, "_create_toolkit", lambda **_kwargs: _FakeToolkit())
    monkeypatch.setattr(worker, "analyze_page_structure", _fake_analyze_page_structure)
    monkeypatch.setattr(worker, "extract_data_with_selectors", _fake_extract_data)
    monkeypatch.setattr(worker, "extract_detail_text_blocks", _empty_async)
    monkeypatch.setattr(worker, "extract_news_links_fallback", _empty_async)
    monkeypatch.setattr(worker, "extract_table_links_fallback", _empty_async)
    monkeypatch.setattr(worker, "explore_for_data_page", _no_navigation)
    monkeypatch.setattr(worker, "validate_data_quality", lambda data, *_args, **_kwargs: {"valid": bool(data)})

    result = asyncio.run(worker.smart_scrape("", "帮我查一下合肥今天天气", limit=3))

    assert result["success"] is True
    assert result["source"] == "https://weather.example.com/good"
    assert opened_tabs == [
        "https://weather.example.com/bad",
        "https://weather.example.com/good",
    ]
    assert closed_tabs["count"] == 1
    assert gather_calls["count"] == 1
    assert closed["value"] is True


def test_smart_scrape_does_not_search_when_explicit_homepage_url_is_provided(monkeypatch):
    worker = WebWorker()
    visited_urls = []

    async def _fake_determine_target_url(_task_description):
        return {
            "url": "",
            "backup_urls": [],
            "need_search": False,
            "search_query": "",
        }

    async def _unexpected_search(*_args, **_kwargs):
        raise AssertionError("search should not be triggered for explicit URL input")

    class _FakeToolkit:
        page = None

        async def create_page(self):
            return None

        async def goto(self, url, **_kwargs):
            visited_urls.append(url)
            return type("Result", (), {"success": True, "data": url})()

        async def human_delay(self, *_args, **_kwargs):
            return None

        async def wait_for_load(self, *_args, **_kwargs):
            return type("Result", (), {"success": True})()

        async def wait_for_selector(self, *_args, **_kwargs):
            return type("Result", (), {"success": True})()

        async def detect_captcha(self):
            return type("Result", (), {"success": True, "data": {"has_captcha": False}})()

        async def scroll_down(self, *_args, **_kwargs):
            return type("Result", (), {"success": True})()

        async def semantic_snapshot(self, *_args, **_kwargs):
            return type("Result", (), {"success": True, "data": {"page_type": "list", "cards": [], "collections": []}})()

        async def get_current_url(self):
            current = visited_urls[-1] if visited_urls else ""
            return type("Result", (), {"success": True, "data": current})()

        async def close(self):
            return None

    async def _fake_analyze_page_structure(_tk, _task_description):
        return {"item_selector": "tr.athing", "fields": {"title": "span.titleline > a"}}

    async def _fake_extract_data(_tk, _config, _limit):
        return [
            {"title": "Story 1", "link": "https://example.com/1"},
            {"title": "Story 2", "link": "https://example.com/2"},
            {"title": "Story 3", "link": "https://example.com/3"},
        ]

    monkeypatch.setattr(worker, "determine_target_url", _fake_determine_target_url)
    monkeypatch.setattr(worker, "gather_search_candidates", _unexpected_search)
    monkeypatch.setattr(worker, "_can_use_static_fetch", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(worker, "_create_toolkit", lambda **_kwargs: _FakeToolkit())
    monkeypatch.setattr(worker, "analyze_page_structure", _fake_analyze_page_structure)
    monkeypatch.setattr(worker, "extract_data_with_selectors", _fake_extract_data)
    monkeypatch.setattr(worker, "validate_data_quality", lambda *_args, **_kwargs: {"valid": True})

    result = asyncio.run(
        worker.smart_scrape(
            "https://news.ycombinator.com/",
            "抓取 https://news.ycombinator.com/ 首页前 3 条新闻",
            limit=3,
        )
    )

    assert result["success"] is True
    assert visited_urls[0] == "https://news.ycombinator.com/"


def test_smart_scrape_skips_url_analysis_when_explicit_non_weather_url_is_provided(monkeypatch):
    worker = WebWorker()
    visited_urls = []

    async def _should_not_run(_task_description):
        raise AssertionError("determine_target_url should not run for explicit non-weather url")

    class _FakeToolkit:
        page = None

        async def create_page(self):
            return None

        async def goto(self, url, **_kwargs):
            visited_urls.append(url)
            return type("Result", (), {"success": True, "data": url})()

        async def human_delay(self, *_args, **_kwargs):
            return None

        async def wait_for_load(self, *_args, **_kwargs):
            return type("Result", (), {"success": True})()

        async def wait_for_selector(self, *_args, **_kwargs):
            return type("Result", (), {"success": True})()

        async def detect_captcha(self):
            return type("Result", (), {"success": True, "data": {"has_captcha": False}})()

        async def scroll_down(self, *_args, **_kwargs):
            return type("Result", (), {"success": True})()

        async def semantic_snapshot(self, *_args, **_kwargs):
            return type("Result", (), {"success": True, "data": {"page_type": "list", "cards": [], "collections": []}})()

        async def get_current_url(self):
            current = visited_urls[-1] if visited_urls else ""
            return type("Result", (), {"success": True, "data": current})()

        async def close(self):
            return None

    async def _fake_analyze_page_structure(_tk, _task_description):
        return {"item_selector": "tr.athing", "fields": {"title": "span.titleline > a"}}

    async def _fake_extract_data(_tk, _config, _limit):
        return [
            {"title": "Story 1", "link": "https://example.com/1"},
            {"title": "Story 2", "link": "https://example.com/2"},
            {"title": "Story 3", "link": "https://example.com/3"},
        ]

    monkeypatch.setattr(worker, "determine_target_url", _should_not_run)
    monkeypatch.setattr(worker, "_can_use_static_fetch", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(worker, "_create_toolkit", lambda **_kwargs: _FakeToolkit())
    monkeypatch.setattr(worker, "analyze_page_structure", _fake_analyze_page_structure)
    monkeypatch.setattr(worker, "extract_data_with_selectors", _fake_extract_data)
    monkeypatch.setattr(worker, "validate_data_quality", lambda *_args, **_kwargs: {"valid": True})

    result = asyncio.run(
        worker.smart_scrape(
            "https://github.com/trending",
            "抓取 GitHub Trending 前 3 条仓库名称和链接",
            limit=3,
        )
    )

    assert result["success"] is True
    assert visited_urls[0] == "https://github.com/trending"


def test_smart_scrape_explicit_url_overrides_need_search_hint(monkeypatch):
    worker = WebWorker()
    visited_urls = []

    async def _fake_determine_target_url(_task_description):
        return {
            "url": "",
            "backup_urls": [],
            "need_search": True,
            "search_query": "site:weather.com.cn 101220101 天气 温度 湿度",
        }

    async def _unexpected_search(*_args, **_kwargs):
        raise AssertionError("search should not run when explicit url is provided")

    class _FakeToolkit:
        page = None

        async def create_page(self):
            return None

        async def goto(self, url, **_kwargs):
            visited_urls.append(url)
            return type("Result", (), {"success": True, "data": url})()

        async def human_delay(self, *_args, **_kwargs):
            return None

        async def wait_for_load(self, *_args, **_kwargs):
            return type("Result", (), {"success": True})()

        async def wait_for_selector(self, *_args, **_kwargs):
            return type("Result", (), {"success": True})()

        async def detect_captcha(self):
            return type("Result", (), {"success": True, "data": {"has_captcha": False}})()

        async def scroll_down(self, *_args, **_kwargs):
            return type("Result", (), {"success": True})()

        async def semantic_snapshot(self, *_args, **_kwargs):
            return type("Result", (), {"success": True, "data": {"page_type": "detail", "cards": [], "collections": []}})()

        async def get_current_url(self):
            current = visited_urls[-1] if visited_urls else ""
            return type("Result", (), {"success": True, "data": current})()

        async def close(self):
            return None

    async def _fake_analyze_page_structure(_tk, _task_description):
        return {"item_selector": ".weather li", "fields": {"text": "text()"}}

    async def _fake_extract_data(_tk, _config, _limit):
        return [
            {"text": "今天 晴 12C"},
            {"text": "湿度 65%"},
            {"text": "风力 3级"},
        ]

    monkeypatch.setattr(worker, "determine_target_url", _fake_determine_target_url)
    monkeypatch.setattr(worker, "gather_search_candidates", _unexpected_search)
    monkeypatch.setattr(worker, "_can_use_static_fetch", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(worker, "_create_toolkit", lambda **_kwargs: _FakeToolkit())
    monkeypatch.setattr(worker, "analyze_page_structure", _fake_analyze_page_structure)
    monkeypatch.setattr(worker, "extract_data_with_selectors", _fake_extract_data)
    monkeypatch.setattr(worker, "validate_data_quality", lambda *_args, **_kwargs: {"valid": True})

    result = asyncio.run(
        worker.smart_scrape(
            "https://www.weather.com.cn/weather/101220101.shtml",
            "抓取今天的天气详情",
            limit=3,
        )
    )

    assert result["success"] is True
    assert visited_urls[0] == "https://www.weather.com.cn/weather/101220101.shtml"


def test_normalize_web_results_extracts_weather_fields_from_text_blocks():
    results = normalize_web_results(
        [
            {"text": "周一 3月16日 多云 17/8°C 东北风3-4级 湿度65% AQI 72"},
            {"text": "生活指数 穿衣建议"},
        ],
        "帮我查一下明天的合肥天气（2026-03-16）",
        limit=3,
        understanding={"page_type": "detail"},
    )

    assert results
    first = results[0]
    assert first["weather"] in {"多云", "cloudy"}
    assert first["temperature"] == "17/8°C"
    assert first["wind"] == "3-4级"
    assert first["humidity"] == "65%"
    assert first["aqi"] == "72"


class _HTMLToolkit:
    def __init__(self, html: str, title: str = "Example Detail"):
        self.html = html
        self.title = title

    async def get_page_html(self):
        return type("Result", (), {"success": True, "data": self.html})()

    async def get_title(self):
        return type("Result", (), {"success": True, "data": self.title})()


def test_extract_detail_text_blocks_normalizes_browser_html():
    worker = WebWorker()
    html = """
    <html><body>
      <main>
        <article>
          <h1>OpenAI API Updates</h1>
          <p>The latest release adds better structured outputs and improved batch processing for production workloads.</p>
          <p>Developers can now inspect richer metadata directly from the response payload.</p>
        </article>
      </main>
    </body></html>
    """

    results = asyncio.run(
        worker.extract_detail_text_blocks(_HTMLToolkit(html, title="OpenAI API Updates"), "总结这篇文章的主要内容", limit=3)
    )

    assert results
    assert results[0]["title"] == "OpenAI API Updates"
    assert "structured outputs" in results[0]["summary"]


def test_smart_scrape_prefers_detail_text_blocks_before_news_fallback(monkeypatch):
    worker = WebWorker()
    visited_urls = []

    class _FakeToolkit:
        page = None

        @staticmethod
        def _result(success=True, data=None, error=""):
            return type("Result", (), {"success": success, "data": data, "error": error})()

        async def create_page(self):
            return None

        async def goto(self, url, **_kwargs):
            visited_urls.append(url)
            return self._result(True, url)

        async def human_delay(self, *_args, **_kwargs):
            return None

        async def wait_for_load(self, *_args, **_kwargs):
            return self._result(True)

        async def wait_for_selector(self, *_args, **_kwargs):
            return self._result(True)

        async def detect_captcha(self):
            return self._result(True, {"has_captcha": False})

        async def scroll_down(self, *_args, **_kwargs):
            return self._result(True)

        async def semantic_snapshot(self, *_args, **_kwargs):
            return self._result(
                True,
                {
                    "page_type": "detail",
                    "regions": [
                        {
                            "ref": "region_1",
                            "kind": "detail",
                            "selector": "main article",
                            "heading": "OpenAI API Updates",
                            "text_sample": "The latest release adds better structured outputs and improved batch processing.",
                            "item_count": 0,
                            "link_count": 2,
                            "control_count": 0,
                            "region": "main",
                            "sample_items": ["The latest release adds better structured outputs."],
                        }
                    ],
                    "cards": [],
                    "collections": [],
                    "controls": [],
                    "elements": [],
                },
            )

        async def get_current_url(self):
            current = visited_urls[-1] if visited_urls else "https://example.com/article"
            return self._result(True, current)

        async def get_page_html(self):
            return self._result(
                True,
                "<html><body><main><article><h1>OpenAI API Updates</h1><p>The latest release adds better structured outputs and improved batch processing for production workloads.</p></article></main></body></html>",
            )

        async def get_title(self):
            return self._result(True, "OpenAI API Updates")

        async def close(self):
            return None

    async def _fake_determine_target_url(_task_description):
        return {"url": "https://example.com/article", "backup_urls": [], "need_search": False, "search_query": ""}

    async def _fake_analyze_page_structure(_tk, _task_description):
        return {"page_type": "detail", "observed_page_type": "detail", "item_selector": "", "fields": {}}

    async def _fake_extract_data(_tk, _config, _limit):
        return []

    async def _unexpected_news_fallback(*_args, **_kwargs):
        raise AssertionError("news fallback should not run before detail text fallback")

    monkeypatch.setattr(worker, "determine_target_url", _fake_determine_target_url)
    monkeypatch.setattr(worker, "_can_use_static_fetch", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(worker, "_create_toolkit", lambda **_kwargs: _FakeToolkit())
    monkeypatch.setattr(worker, "analyze_page_structure", _fake_analyze_page_structure)
    monkeypatch.setattr(worker, "extract_data_with_selectors", _fake_extract_data)
    monkeypatch.setattr(worker, "extract_news_links_fallback", _unexpected_news_fallback)

    result = asyncio.run(
        worker.smart_scrape(
            "https://example.com/article",
            "总结这篇文章的主要内容",
            limit=3,
        )
    )

    assert result["success"] is True
    assert result["data"]
    assert result["data"][0]["title"] == "OpenAI API Updates"
    assert "structured outputs" in result["data"][0]["summary"]
