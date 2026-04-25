import asyncio
import base64

from agents.web_worker import WebWorker


def test_decode_bing_redirect_url_with_base64_payload():
    target = "https://www.reuters.com/world/middle-east/"
    encoded = base64.urlsafe_b64encode(target.encode("utf-8")).decode("ascii").rstrip("=")
    redirect = f"https://www.bing.com/ck/a?u=a1{encoded}"
    decoded = WebWorker._decode_redirect_url(redirect)
    assert decoded == target


def test_snapshot_cards_to_search_cards_prefers_target_url_for_intermediary_links():
    worker = WebWorker()

    cards = worker._snapshot_cards_to_search_cards(
        {
            "cards": [
                {
                    "title": "合肥天气",
                    "link": "https://www.baidu.com/link?url=opaque-token",
                    "target_url": "https://weather.example.com/hefei",
                    "snippet": "今天晴，12℃",
                    "target_ref": "el_1",
                }
            ]
        }
    )

    assert cards == [
        {
            "title": "合肥天气",
            "link": "https://weather.example.com/hefei",
            "raw_link": "https://www.baidu.com/link?url=opaque-token",
            "target_url": "https://weather.example.com/hefei",
            "source": "",
            "date": "",
            "snippet": "今天晴，12℃",
            "target_ref": "el_1",
            "target_selector": "",
        }
    ]


def test_search_engine_domain_filter():
    assert WebWorker._is_search_engine_domain("https://www.bing.com/search?q=test") is True
    assert WebWorker._is_search_engine_domain("https://www.reuters.com/world/") is False


def test_format_semantic_snapshot_for_llm_includes_main_text_blocks_and_stage():
    worker = WebWorker()

    text = asyncio.run(worker._format_semantic_snapshot_for_llm(
        {
            "page_type": "detail",
            "page_stage": "extracting",
            "url": "https://example.com/article",
            "title": "Example Article",
            "main_text": "This is the main article text.",
            "visible_text_blocks": [
                {"kind": "p", "text": "First paragraph", "selector": "article p:first-child"},
                {"kind": "li", "text": "Key bullet", "selector": "article li"},
            ],
            "blocked_signals": ["body:captcha"],
        }
    ))

    assert "页面阶段: extracting" in text
    assert "主体文本: This is the main article text." in text
    assert "可见文本块:" in text
    assert "First paragraph" in text
    assert "阻塞信号: body:captcha" in text


class _PlannerLLM:
    def __init__(self, payload):
        self.payload = payload

    def chat_with_system(self, **_kwargs):
        return {"ok": True}

    def parse_json_response(self, _response):
        return self.payload


class _CaptureLLM(_PlannerLLM):
    def __init__(self, payload):
        super().__init__(payload)
        self.last_system_prompt = ""
        self.last_user_message = ""

    def chat_with_system(self, **kwargs):
        self.last_system_prompt = str(kwargs.get("system_prompt", "") or "")
        self.last_user_message = str(kwargs.get("user_message", "") or "")
        return {"ok": True}


class _FailingPlannerLLM:
    def chat_with_system(self, **_kwargs):
        raise RuntimeError("no llm")

    def parse_json_response(self, _response):
        return {}


class _SemanticSnapshotToolkit:
    def __init__(self, snapshot, url="https://www.bing.com/search?q=test", html="<html><body>search results</body></html>"):
        self.snapshot = snapshot
        self.url = url
        self.html = html
        self.clicked_ref = ""
        self.goto_url = ""

    async def semantic_snapshot(self, max_elements=80, include_cards=True):
        del max_elements, include_cards
        return type("Result", (), {"success": True, "data": self.snapshot})()

    async def click_ref(self, ref):
        self.clicked_ref = ref
        return type("Result", (), {"success": True})()

    async def goto(self, url, timeout=30000):
        del timeout
        self.goto_url = url
        self.url = url
        return type("Result", (), {"success": True, "data": url})()

    async def human_delay(self, *_args, **_kwargs):
        return None

    async def wait_for_load(self, *_args, **_kwargs):
        return type("Result", (), {"success": True})()

    async def wait_for_page_type_change(self, *_args, **_kwargs):
        return type("Result", (), {"success": True, "data": "detail"})()

    async def get_current_url(self):
        return type("Result", (), {"success": True, "data": self.url})()

    async def get_page_html(self):
        return type("Result", (), {"success": True, "data": self.html})()


class _DummyPageStructure:
    main_content_blocks = [object()]
    interactive_elements = [object()]

    def to_llm_prompt(self):
        return "# 页面：测试页\n\n### 主要内容区域\n1. [paragraph] 示例内容\n   选择器: main"


class _TableFallbackToolkit:
    async def evaluate_js(self, _script, _arg=None):
        del _script, _arg
        return type(
            "Result",
            (),
            {
                "success": True,
                "data": [
                    {"title": "仅标题", "link": "https://www.cnvd.org.cn/header", "row_text": "仅标题 CVE BID"},
                    {
                        "title": "CNVD-2026-00001 某系统远程代码执行漏洞",
                        "link": "https://www.cnvd.org.cn/flaw/show/CNVD-2026-00001",
                        "row_text": "CNVD-2026-00001 某系统远程代码执行漏洞 高危 2026-03-15",
                    },
                    {
                        "title": "CNVD-2026-00002 某组件权限提升漏洞",
                        "link": "https://www.cnvd.org.cn/flaw/show/CNVD-2026-00002",
                        "row_text": "CNVD-2026-00002 某组件权限提升漏洞 中危 2026-03-14",
                    },
                ],
            },
        )()


def test_plan_search_queries_uses_llm_planned_short_queries():
    worker = WebWorker(llm_client=_PlannerLLM({"queries": ["US Iran strikes Reuters", "site:bbc.com US Iran conflict"]}))

    queries = worker.plan_search_queries(
        "定向检索 Reuters 与 BBC 关于近期美国与伊朗行动的直接报道，并核实 war wording",
        base_query="recent US Iran conflict news authoritative sources",
    )

    assert queries == ["US Iran strikes Reuters", "site:bbc.com US Iran conflict"]


def test_plan_search_queries_dedupes_near_duplicate_queries():
    worker = WebWorker(
        llm_client=_PlannerLLM(
            {
                "queries": [
                    "合肥 今天 天气",
                    "合肥 天气预报 今天",
                    "合肥 今日 天气",
                ]
            }
        )
    )

    queries = worker.plan_search_queries(
        "帮我查一下合肥今天天气",
        base_query="合肥 今天 天气",
    )

    assert queries == ["合肥 今天 天气"]


def test_plan_search_queries_limits_detail_style_tasks_to_single_query():
    worker = WebWorker(
        llm_client=_PlannerLLM(
            {
                "queries": [
                    "合肥 天气",
                    "合肥 今天 天气 预报",
                    "合肥 今日 天气",
                ]
            }
        )
    )

    queries = worker.plan_search_queries(
        "帮我查一下合肥今天天气详情",
        base_query="合肥 今天 天气",
        max_queries=4,
    )

    assert queries == ["合肥 天气"]


def test_extract_table_links_fallback_filters_header_rows_for_vulnerability_lists():
    worker = WebWorker()

    data = asyncio.run(
        worker.extract_table_links_fallback(
            _TableFallbackToolkit(),
            "抓取前 2 条漏洞标题和链接",
            limit=2,
        )
    )

    assert data == [
        {
            "title": "CNVD-2026-00001 某系统远程代码执行漏洞",
            "link": "https://www.cnvd.org.cn/flaw/show/CNVD-2026-00001",
        },
        {
            "title": "CNVD-2026-00002 某组件权限提升漏洞",
            "link": "https://www.cnvd.org.cn/flaw/show/CNVD-2026-00002",
        },
    ]


def test_rerank_search_results_prefers_llm_selected_cards():
    worker = WebWorker(
        llm_client=_PlannerLLM(
            {
                "selected_indexes": [2],
                "serp_sufficient": False,
            }
        )
    )
    cards = [
        {"title": "How to resize a submit button", "link": "https://stackoverflow.com/q/1", "source": "Stack Overflow", "snippet": "Unrelated UI question"},
        {"title": "US strikes in Iran raise tensions", "link": "https://www.reuters.com/world/article-123", "source": "Reuters", "snippet": "The report describes direct military action."},
    ]

    ranked, serp_sufficient = worker._rerank_search_results(
        "核实近期美国与伊朗之间军事行动",
        "US Iran strikes Reuters",
        cards,
        max_results=2,
    )

    assert serp_sufficient is False
    assert ranked[0]["link"] == "https://www.reuters.com/world/article-123"


def test_openai_model_search_ranking_prefers_official_sources_without_llm():
    worker = WebWorker(llm_client=_FailingPlannerLLM())
    cards = [
        {
            "title": "GPT-5.2 summary",
            "link": "https://example-news.test/openai-gpt-5-2",
            "source": "Example News",
            "snippet": "A news summary of OpenAI's latest model.",
        },
        {
            "title": "Introducing GPT-5.2",
            "link": "https://openai.com/index/introducing-gpt-5-2/",
            "source": "OpenAI",
            "snippet": "OpenAI official model announcement.",
        },
    ]

    ranked, _ = worker._rerank_search_results(
        "帮我看下 OpenAI 最新模型，并给我信息源",
        "OpenAI latest models",
        cards,
        max_results=2,
    )

    assert ranked[0]["link"].startswith("https://openai.com/")


def test_openai_latest_model_queries_include_official_domain_fallback():
    worker = WebWorker(llm_client=_PlannerLLM({"queries": ["OpenAI latest model"]}))

    queries = worker.plan_search_queries(
        "帮我看下OpenAI最新模型，总结中文并给我信息源",
        max_queries=3,
    )

    assert "OpenAI latest model" in queries
    assert any(query.startswith("site:openai.com") for query in queries)


def test_openai_latest_model_queries_include_current_year_and_time_context():
    llm = _CaptureLLM({"queries": ["OpenAI latest model"]})
    worker = WebWorker(llm_client=llm)

    queries = worker.plan_search_queries(
        "帮我看下OpenAI最新模型，总结中文并给我信息源",
        max_queries=4,
        current_time_context={"local_date": "2026-04-24", "current_year": 2026},
    )

    assert any("2026" in query or "2026-04-24" in query for query in queries)
    assert any(query.startswith("site:openai.com") for query in queries)
    assert "2026-04-24" in llm.last_user_message


def test_rerank_search_results_receives_current_time_context():
    llm = _CaptureLLM({"selected_indexes": [1], "serp_sufficient": False})
    worker = WebWorker(llm_client=llm)
    cards = [
        {
            "title": "Introducing GPT-5.2",
            "link": "https://openai.com/index/introducing-gpt-5-2/",
            "source": "OpenAI",
            "snippet": "OpenAI official model announcement.",
        },
        {
            "title": "Older model roundup",
            "link": "https://example.com/openai-models",
            "source": "Example",
            "snippet": "A 2025 model roundup.",
        },
    ]

    ranked, _ = worker._rerank_search_results(
        "帮我看下OpenAI最新模型，总结中文并给我信息源",
        "OpenAI latest model 2026",
        cards,
        max_results=2,
        current_time_context={"local_date": "2026-04-24", "current_year": 2026},
    )

    assert ranked[0]["link"].startswith("https://openai.com/")
    assert "2026-04-24" in llm.last_user_message


def test_search_for_result_cards_honors_headless_override(monkeypatch):
    captured = []

    class _FakeToolkit:
        def __init__(self):
            self.current_url = ""

        async def create_page(self):
            return None

        async def goto(self, url):
            self.current_url = url
            return type("Result", (), {"success": True})()

        async def human_delay(self, *_args, **_kwargs):
            return None

        async def wait_for_load(self, *_args, **_kwargs):
            return type("Result", (), {"success": True})()

        async def wait_for_selector(self, *_args, **_kwargs):
            return type("Result", (), {"success": True})()

        async def element_exists(self, *_args, **_kwargs):
            return type("Result", (), {"success": True, "data": True})()

        async def type_text(self, *_args, **_kwargs):
            return type("Result", (), {"success": True})()

        async def press_key(self, *_args, **_kwargs):
            return type("Result", (), {"success": True})()

        async def get_current_url(self):
            return type("Result", (), {"success": True, "data": self.current_url})()

        async def evaluate_js(self, _script, _arg=None):
            if isinstance(_arg, str):
                return type("Result", (), {"success": True, "data": {"matches": 1, "textLength": 1200}})()
            return type(
                "Result",
                (),
                {
                    "success": True,
                    "data": [
                        {
                            "title": "US strikes in Iran raise tensions",
                            "link": "https://www.reuters.com/world/article-123",
                            "source": "Reuters",
                            "date": "Mar 10",
                            "snippet": "Direct military action was reported.",
                        }
                    ],
                },
            )()

        async def close(self):
            return None

    worker = WebWorker(llm_client=_PlannerLLM({"selected_indexes": [1], "serp_sufficient": False}))

    def _fake_create_toolkit(headless=True):
        captured.append(headless)
        return _FakeToolkit()

    monkeypatch.setattr(worker, "_create_toolkit", _fake_create_toolkit)

    cards = asyncio.run(
        worker.search_for_result_cards(
            "US Iran strikes Reuters",
            task_description="核实近期美国与伊朗之间军事行动",
            headless=False,
            max_results=1,
        )
    )

    assert captured == [False]
    assert cards[0]["link"] == "https://www.reuters.com/world/article-123"


def test_search_for_result_cards_falls_back_when_duckduckgo_is_blank(monkeypatch):
    visited = []

    class _FakeToolkit:
        def __init__(self):
            self.current_url = ""

        async def create_page(self):
            return None

        async def goto(self, url):
            self.current_url = url
            visited.append(url)
            return type("Result", (), {"success": True})()

        async def human_delay(self, *_args, **_kwargs):
            return None

        async def wait_for_load(self, *_args, **_kwargs):
            return type("Result", (), {"success": True})()

        async def wait_for_selector(self, *_args, **_kwargs):
            if "bing.com" in self.current_url:
                return type("Result", (), {"success": True})()
            return type("Result", (), {"success": False})()

        async def element_exists(self, *_args, **_kwargs):
            return type("Result", (), {"success": True, "data": True})()

        async def type_text(self, *_args, **_kwargs):
            return type("Result", (), {"success": True})()

        async def press_key(self, *_args, **_kwargs):
            return type("Result", (), {"success": True})()

        async def get_current_url(self):
            return type("Result", (), {"success": True, "data": self.current_url})()

        async def evaluate_js(self, _script, arg=None):
            if isinstance(arg, str):
                if "google.com" in self.current_url:
                    return type("Result", (), {"success": True, "data": {"matches": 1, "textLength": 1200}})()
                return type("Result", (), {"success": True, "data": {"matches": 0, "textLength": 30}})()

            if "google.com" in self.current_url:
                return type(
                    "Result",
                    (),
                    {
                        "success": True,
                        "data": [
                            {
                                "title": "US strikes in Iran raise tensions",
                                "link": "https://www.reuters.com/world/article-123",
                                "source": "Reuters",
                                "date": "Mar 10",
                                "snippet": "Direct military action was reported.",
                            }
                        ],
                    },
                )()
            return type("Result", (), {"success": True, "data": []})()

        async def close(self):
            return None

    worker = WebWorker(llm_client=_PlannerLLM({"selected_indexes": [1], "serp_sufficient": False}))

    monkeypatch.setattr(worker, "_create_toolkit", lambda headless=True: _FakeToolkit())

    cards = asyncio.run(
        worker.search_for_result_cards(
            "US Iran strikes Reuters",
            task_description="核实近期美国与伊朗之间军事行动",
            headless=False,
            max_results=1,
        )
    )

    assert any("google.com" in url for url in visited)
    assert any("duckduckgo.com" in url for url in visited)
    assert cards[0]["link"] == "https://www.reuters.com/world/article-123"


async def _async_dummy_page_structure(*_args, **_kwargs):
    return _DummyPageStructure()


def test_semantic_search_results_prefers_click_ref_navigation():
    worker = WebWorker(llm_client=_PlannerLLM({"selected_indexes": [1], "serp_sufficient": False}))
    worker.validate_data_quality = lambda *_args, **_kwargs: {"valid": False}
    toolkit = _SemanticSnapshotToolkit(
        {
            "page_type": "serp",
            "cards": [
                {
                    "title": "Reuters: Public appearances indicate Khamenei is alive",
                    "link": "https://www.reuters.com/world/middle-east/khamenei-update",
                    "source": "Reuters",
                    "snippet": "Recent public appearances indicate the reports are false.",
                    "target_ref": "el_2",
                    "target_selector": "a.result",
                }
            ],
            "elements": [],
        }
    )

    result = asyncio.run(
        worker._maybe_handle_semantic_search_results(
            toolkit,
            "核实阿亚图拉阿里哈梅内伊是否在最近冲突中死亡",
            limit=1,
        )
    )

    assert result["handled"] is True
    assert result["navigated"] is True
    assert toolkit.clicked_ref == "el_2"


def test_semantic_search_results_can_return_cards_when_snippets_are_sufficient():
    worker = WebWorker(llm_client=_PlannerLLM({"selected_indexes": [1], "serp_sufficient": True}))
    worker.validate_data_quality = lambda *_args, **_kwargs: {"valid": True}
    toolkit = _SemanticSnapshotToolkit(
        {
            "page_type": "serp",
            "cards": [
                {
                    "title": "Reuters: Public appearances indicate Khamenei is alive",
                    "link": "https://www.reuters.com/world/middle-east/khamenei-update",
                    "source": "Reuters",
                    "snippet": "Recent public appearances indicate the reports are false.",
                    "target_ref": "el_2",
                    "target_selector": "a.result",
                }
            ],
            "elements": [],
        }
    )

    result = asyncio.run(
        worker._maybe_handle_semantic_search_results(
            toolkit,
            "核实阿亚图拉阿里哈梅内伊是否在最近冲突中死亡",
            limit=1,
        )
    )

    assert result["handled"] is True
    assert result["navigated"] is True
    assert toolkit.clicked_ref == "el_2"


def test_semantic_search_results_do_not_short_circuit_detail_tasks_even_when_serp_is_marked_sufficient():
    worker = WebWorker(llm_client=_PlannerLLM({"selected_indexes": [1], "serp_sufficient": True}))
    worker.validate_data_quality = lambda *_args, **_kwargs: {"valid": True}
    toolkit = _SemanticSnapshotToolkit(
        {
            "page_type": "serp",
            "cards": [
                {
                    "title": "合肥天气详情",
                    "link": "https://weather.example.com/hefei",
                    "source": "Example Weather",
                    "snippet": "今天小雨，6~11°C，东北风3级。",
                    "target_ref": "el_weather",
                    "target_selector": "a.weather",
                }
            ],
            "elements": [],
        },
        url="https://www.baidu.com/s?wd=%E5%90%88%E8%82%A5%E5%A4%A9%E6%B0%94",
    )

    result = asyncio.run(
        worker._maybe_handle_semantic_search_results(
            toolkit,
            "帮我查一下合肥今天天气详情",
            limit=1,
        )
    )

    assert result["handled"] is True
    assert result["navigated"] is True
    assert toolkit.clicked_ref == "el_weather"


def test_semantic_search_results_open_real_result_for_latest_source_task():
    worker = WebWorker(llm_client=_PlannerLLM({"selected_indexes": [1], "serp_sufficient": True}))
    worker.validate_data_quality = lambda *_args, **_kwargs: {"valid": True}
    toolkit = _SemanticSnapshotToolkit(
        {
            "page_type": "serp",
            "cards": [
                {
                    "title": "Introducing GPT-5.2",
                    "link": "https://openai.com/index/introducing-gpt-5-2/",
                    "source": "OpenAI",
                    "snippet": "GPT-5.2 is OpenAI's latest model.",
                    "target_ref": "el_openai",
                    "target_selector": "a.result",
                }
            ],
            "elements": [],
        }
    )

    result = asyncio.run(
        worker._maybe_handle_semantic_search_results(
            toolkit,
            "帮我看下OpenAI最新模型，总结中文并给我信息源",
            limit=1,
        )
    )

    assert result["handled"] is True
    assert result["navigated"] is True
    assert toolkit.clicked_ref == "el_openai"


def test_semantic_search_results_can_return_cards_for_list_tasks_when_snippets_are_sufficient():
    worker = WebWorker(llm_client=_PlannerLLM({"selected_indexes": [1], "serp_sufficient": True}))
    worker.validate_data_quality = lambda *_args, **_kwargs: {"valid": True}
    toolkit = _SemanticSnapshotToolkit(
        {
            "page_type": "serp",
            "cards": [
                {
                    "title": "Reuters: Public appearances indicate Khamenei is alive",
                    "link": "https://www.reuters.com/world/middle-east/khamenei-update",
                    "source": "Reuters",
                    "snippet": "Recent public appearances indicate the reports are false.",
                    "target_ref": "el_2",
                    "target_selector": "a.result",
                }
            ],
            "elements": [],
        }
    )

    result = asyncio.run(
        worker._maybe_handle_semantic_search_results(
            toolkit,
            "列出最近相关报道的标题和链接",
            limit=1,
        )
    )

    assert result["handled"] is True
    assert result["return_data"] is True
    assert result["data"][0]["link"] == "https://www.reuters.com/world/middle-east/khamenei-update"


def test_analyze_page_structure_includes_semantic_snapshot_context():
    llm = _CaptureLLM({"success": True, "item_selector": "article", "fields": {"title": "h2 a"}})
    worker = WebWorker(llm_client=llm)
    worker.page_perceiver.perceive_page = _async_dummy_page_structure
    toolkit = _SemanticSnapshotToolkit(
        {
            "page_type": "detail",
            "url": "https://www.reuters.com/world/middle-east/khamenei-update",
            "title": "Reuters",
            "regions": [
                {
                    "ref": "region_1",
                    "kind": "detail",
                    "selector": "article",
                    "heading": "Khamenei update",
                    "text_sample": "Recent public appearances indicate the reports are false.",
                    "sample_items": ["Recent public appearances indicate the reports are false."],
                    "item_count": 0,
                    "link_count": 1,
                    "control_count": 0,
                    "region": "main",
                }
            ],
            "cards": [],
            "elements": [
                {"ref": "el_1", "type": "link", "text": "Full story", "label": "", "selector": "a.story"}
            ],
            "affordances": {"has_search_box": False, "has_pagination": False},
        },
        url="https://www.reuters.com/world/middle-east/khamenei-update",
        html="<html><body><article><h2><a>Full story</a></h2></article></body></html>",
    )

    config = asyncio.run(worker.analyze_page_structure(toolkit, "提取文章标题和链接"))

    assert config["item_selector"] == "article"
    assert "页面类型: detail" in llm.last_system_prompt
    assert "页面区域:" in llm.last_system_prompt
    assert "Task-ranked candidate regions:" in llm.last_system_prompt
    assert "kind=detail ref=region_1" in llm.last_system_prompt
    assert "可见关键元素:" in llm.last_system_prompt


def test_analyze_page_structure_normalizes_hacker_news_rows():
    llm = _CaptureLLM({"success": True, "item_selector": "tr", "fields": {"title": "a"}})
    worker = WebWorker(llm_client=llm)
    worker.page_perceiver.perceive_page = _async_dummy_page_structure
    toolkit = _SemanticSnapshotToolkit(
        {
            "page_type": "list",
            "url": "https://news.ycombinator.com/",
            "title": "Hacker News",
            "cards": [],
            "elements": [],
            "affordances": {"has_search_box": False, "has_pagination": True},
        },
        url="https://news.ycombinator.com/",
        html="<html><body><table><tr class='athing' id='1'><td class='title'><span class='titleline'><a href='https://example.com/1'>Story 1</a></span></td></tr></table></body></html>",
    )

    config = asyncio.run(worker.analyze_page_structure(toolkit, "抓取前 3 条新闻的标题和链接"))

    assert config["item_selector"] == "tr.athing"
    assert config["fields"]["title"] == "a"
    assert config["fields"]["id"] == "@id"


def test_try_next_page_via_toolkit_prefers_semantic_load_more_ref():
    worker = WebWorker()
    toolkit = _SemanticSnapshotToolkit(
        {
            "page_type": "list",
            "cards": [],
            "elements": [],
            "affordances": {
                "has_load_more": True,
                "load_more_ref": "ctl_load_more",
                "has_pagination": True,
                "next_page_ref": "ctl_next_page",
            },
        },
        url="https://example.com/list",
    )

    success = asyncio.run(worker._try_next_page_via_toolkit(toolkit))

    assert success is True
    assert toolkit.clicked_ref == "ctl_load_more"


def test_looks_like_search_results_url_requires_real_serp_path():
    worker = WebWorker()

    assert worker._looks_like_search_results_url("https://www.bing.com/search?q=合肥+明天+天气") is True
    assert worker._looks_like_search_results_url("https://www.bing.com/?form=QBLH") is False
    assert worker._looks_like_search_results_url("https://www.baidu.com/s?wd=%E5%90%88%E8%82%A5%E5%A4%A9%E6%B0%94") is True
    assert worker._looks_like_search_results_url("https://www.baidu.com/") is False


def test_search_for_result_cards_skips_engine_when_not_on_results_page():
    class _SearchResponse:
        def __init__(self, success=False, results=None, error=''):
            self.success = success
            self.results = results or []
            self.error = error

    class _FakeToolkit:
        def __init__(self):
            self.current_url = ''

        async def create_page(self):
            return None

        async def goto(self, url):
            self.current_url = url
            return type('Result', (), {'success': True})()

        async def human_delay(self, *_args, **_kwargs):
            return None

        async def wait_for_load(self, *_args, **_kwargs):
            return type('Result', (), {'success': True})()

        async def element_exists(self, *_args, **_kwargs):
            return type('Result', (), {'success': True, 'data': True})()

        async def type_text(self, *_args, **_kwargs):
            return type('Result', (), {'success': True})()

        async def press_key(self, *_args, **_kwargs):
            return type('Result', (), {'success': True})()

        async def get_current_url(self):
            homepage = self.current_url.split('/search', 1)[0].split('/s?', 1)[0]
            return type('Result', (), {'success': True, 'data': homepage})()

        async def get_title(self):
            return type('Result', (), {'success': True, 'data': 'Bing'})()

        async def evaluate_js(self, _script, _arg=None):
            return type('Result', (), {'success': True, 'data': {'matches': 1, 'textLength': 1500, 'bodyText': 'homepage'}})()

        async def close(self):
            return None

    worker = WebWorker(llm_client=_PlannerLLM({'selected_indexes': [1], 'serp_sufficient': False}))
    worker._create_toolkit = lambda headless=True: _FakeToolkit()

    async def _fake_search(*_args, **_kwargs):
        return _SearchResponse(success=False, error='disabled')

    worker.search_engine_manager.search = _fake_search

    async def _fail_extract(*_args, **_kwargs):
        raise AssertionError('extract_search_result_cards should not run when results page is not ready')

    worker._extract_search_result_cards = _fail_extract

    cards = asyncio.run(
        worker.search_for_result_cards(
            '合肥 明天 天气',
            task_description='帮我查一下明天的合肥天气',
            max_results=2,
            headless=False,
        )
    )

    assert cards == []


def test_wait_for_search_results_ready_accepts_semantic_snapshot_results():
    class _Toolkit:
        async def wait_for_load(self, *_args, **_kwargs):
            return type("Result", (), {"success": True})()

        async def get_current_url(self):
            return type("Result", (), {"success": True, "data": "https://www.baidu.com/s?wd=%E5%90%88%E8%82%A5%E5%A4%A9%E6%B0%94"})()

        async def get_title(self):
            return type("Result", (), {"success": True, "data": "合肥天气_百度搜索"})()

        async def evaluate_js(self, _script, _arg=None):
            return type(
                "Result",
                (),
                {
                    "success": True,
                    "data": {
                        "matches": 0,
                        "textLength": 640,
                        "bodyText": "合肥天气 今天晴 12℃ 北风 3级",
                    },
                },
            )()

        async def semantic_snapshot(self, *_args, **_kwargs):
            return type(
                "Result",
                (),
                {
                    "success": True,
                    "data": {
                        "page_type": "serp",
                        "cards": [
                            {
                                "title": "合肥天气",
                                "link": "https://www.baidu.com/link?url=opaque-token",
                                "target_url": "https://weather.example.com/hefei",
                            }
                        ],
                        "affordances": {
                            "has_results": True,
                            "collection_item_count": 1,
                        },
                    },
                },
            )()

        async def human_delay(self, *_args, **_kwargs):
            return None

        async def get_page_html(self):
            return type("Result", (), {"success": True, "data": "<html></html>"})()

    worker = WebWorker()

    ready = asyncio.run(
        worker._wait_for_search_results_ready(
            _Toolkit(),
            "https://www.baidu.com/s?wd=%E5%90%88%E8%82%A5%E5%A4%A9%E6%B0%94",
        )
    )

    assert ready is True


def test_search_for_result_cards_prefers_semantic_snapshot_for_baidu_results(monkeypatch):
    class _SearchResponse:
        def __init__(self, success=False, results=None, error=""):
            self.success = success
            self.results = results or []
            self.error = error

    class _FakeToolkit:
        def __init__(self):
            self.current_url = ""

        async def create_page(self):
            return None

        async def goto(self, url):
            self.current_url = url
            return type("Result", (), {"success": True})()

        async def human_delay(self, *_args, **_kwargs):
            return None

        async def wait_for_load(self, *_args, **_kwargs):
            return type("Result", (), {"success": True})()

        async def element_exists(self, *_args, **_kwargs):
            return type("Result", (), {"success": True, "data": True})()

        async def type_text(self, *_args, **_kwargs):
            return type("Result", (), {"success": True})()

        async def press_key(self, *_args, **_kwargs):
            return type("Result", (), {"success": True})()

        async def get_current_url(self):
            return type("Result", (), {"success": True, "data": self.current_url})()

        async def get_title(self):
            title = "合肥天气_百度搜索" if "baidu.com" in self.current_url else "Search"
            return type("Result", (), {"success": True, "data": title})()

        async def evaluate_js(self, _script, arg=None):
            if isinstance(arg, str):
                if "baidu.com" in self.current_url:
                    return type(
                        "Result",
                        (),
                        {"success": True, "data": {"matches": 0, "textLength": 640, "bodyText": "合肥天气 今天晴 12℃"}},
                    )()
                return type(
                    "Result",
                    (),
                    {"success": True, "data": {"matches": 0, "textLength": 24, "bodyText": "homepage"}},
                )()
            return type("Result", (), {"success": True, "data": []})()

        async def semantic_snapshot(self, *_args, **_kwargs):
            if "baidu.com" in self.current_url:
                return type(
                    "Result",
                    (),
                    {
                        "success": True,
                        "data": {
                            "page_type": "serp",
                            "cards": [
                                {
                                    "title": "合肥天气",
                                    "link": "https://www.baidu.com/link?url=opaque-token",
                                    "target_url": "https://weather.example.com/hefei",
                                    "source": "天气网",
                                    "snippet": "今天晴，12℃",
                                    "target_ref": "el_weather",
                                }
                            ],
                            "affordances": {"has_results": True, "collection_item_count": 1},
                        },
                    },
                )()
            return type(
                "Result",
                (),
                {"success": True, "data": {"page_type": "unknown", "cards": [], "affordances": {}}},
            )()

        async def get_page_html(self):
            return type("Result", (), {"success": True, "data": "<html></html>"})()

        async def close(self):
            return None

    worker = WebWorker(llm_client=_PlannerLLM({"selected_indexes": [1], "serp_sufficient": False}))
    worker._create_toolkit = lambda headless=True: _FakeToolkit()

    async def _fake_search(*_args, **_kwargs):
        return _SearchResponse(success=False, error="disabled")

    worker.search_engine_manager.search = _fake_search

    cards = asyncio.run(
        worker.search_for_result_cards(
            "合肥今天 天气",
            task_description="帮我查一下合肥今天的天气",
            max_results=1,
            headless=False,
        )
    )

    assert len(cards) == 1
    assert cards[0]["title"] == "合肥天气"
    assert cards[0]["url"] == "https://weather.example.com/hefei"
    assert cards[0]["link"] == "https://weather.example.com/hefei"
    assert cards[0]["summary"] == "今天晴，12℃"
    assert cards[0]["source"] == "天气网"
    assert cards[0]["target_ref"] == "el_weather"


def test_direct_url_strategy_does_not_fake_google_success_for_unknown_queries():
    from utils.search_engine import DirectURLSearchEngine

    engine = DirectURLSearchEngine()
    response = asyncio.run(engine.search("合肥 明天 天气"))

    assert response.success is False
    assert response.results == []
    assert "infer target site" in str(response.error)
