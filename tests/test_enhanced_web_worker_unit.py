import asyncio

from agents.enhanced_web_worker import EnhancedWebWorker


class _DummyLLM:
    def chat_with_system(self, *_args, **_kwargs):
        raise AssertionError("LLM should not be called in this test")

    def parse_json_response(self, _response):
        return {}


class _Result:
    def __init__(self, success=True, data=None, error=""):
        self.success = success
        self.data = data
        self.error = error


class _FakeNode:
    def __init__(self, tag: str, text: str = "", attrs=None):
        self.tag = tag
        self.text = text
        self.attrs = attrs or {}

    async def inner_text(self):
        return self.text

    async def text_content(self):
        return self.text

    async def evaluate(self, _script):
        return self.tag

    async def get_attribute(self, name):
        return self.attrs.get(name)


class _FakeItem(_FakeNode):
    def __init__(self):
        super().__init__("article")

    async def query_selector(self, selector):
        if selector == "a":
            return _FakeNode("a", "OpenViking", {"href": "/volcengine/OpenViking"})
        if selector == "p":
            return _FakeNode("p", "AI agent runtime")
        return None

    async def query_selector_all(self, selector):
        del selector
        return []


class _FakeToolkit:
    async def query_all(self, selector):
        assert selector == "article"
        return _Result(True, [_FakeItem()])

    async def get_current_url(self):
        return _Result(True, "https://github.com/trending")

    async def click(self, _selector):
        return _Result(True)

    async def human_delay(self, *_args, **_kwargs):
        return _Result(True)


class _PartialItem(_FakeNode):
    def __init__(self, title: str = "", href: str = ""):
        super().__init__("article")
        self.title = title
        self.href = href

    async def query_selector(self, selector):
        if selector == "a":
            return _FakeNode("a", self.title, {"href": self.href})
        return None

    async def query_selector_all(self, selector):
        del selector
        return []


class _MixedToolkit(_FakeToolkit):
    async def query_all(self, selector):
        assert selector == "article"
        return _Result(
            True,
            [
                _PartialItem("", "/noise-1"),
                _PartialItem("", "/noise-2"),
                _PartialItem("OpenViking", "/volcengine/OpenViking"),
                _PartialItem("claude-plugins-official", "/anthropics/claude-plugins-official"),
                _PartialItem("dimos", "/dimensionalOS/dimos"),
            ],
        )


class _SemanticOnlyWorker(EnhancedWebWorker):
    async def _understand_page(self, toolkit, task_description):
        del toolkit, task_description
        return {
            "success": True,
            "page_type": "serp",
            "main_function": "展示搜索结果",
            "key_fields": ["title", "url", "summary"],
            "semantic_snapshot": {
                "page_type": "serp",
                "cards": [
                    {
                        "title": "OpenAI API 发布更新",
                        "link": "https://openai.com/index/openai-api/",
                        "source": "OpenAI",
                        "date": "2026-03-15",
                        "snippet": "官方发布了新的 API 能力。",
                    },
                    {
                        "title": "LangGraph 发布新版本",
                        "link": "https://example.com/langgraph",
                        "source": "Example",
                        "date": "2026-03-14",
                        "snippet": "更新了 agent workflow 能力。",
                    },
                ],
            },
        }

    async def _generate_selectors(self, toolkit, task_description, understanding):
        del toolkit, task_description, understanding
        raise AssertionError("selector generation should not run for semantic cards")


class _SerpDomToolkit:
    async def evaluate_js(self, _script, _scan_limit):
        return _Result(
            True,
            [
                {
                    "title": "OpenAI API",
                    "link": "https://www.bing.com/images/search?view=detailV2&id=abc&mediaurl=https://images.example.com/openai.png&q=openai+api&idpp=rc",
                    "snippet": "图片详情页",
                },
                {
                    "title": "API Platform | OpenAI",
                    "link": "https://openai.com/api/",
                    "snippet": "Build with OpenAI API.",
                    "source": "OpenAI",
                },
                {
                    "title": "OpenAI developer quickstart",
                    "link": "https://platform.openai.com/docs/quickstart",
                    "snippet": "Quickstart guide.",
                    "source": "OpenAI Docs",
                },
            ],
        )


class _SerpDomWorker(EnhancedWebWorker):
    async def _understand_page(self, toolkit, task_description):
        del toolkit, task_description
        return {
            "success": True,
            "page_type": "serp",
            "main_function": "展示搜索结果",
            "key_fields": ["title", "url"],
            "semantic_snapshot": {"page_type": "serp", "cards": []},
        }

    async def _generate_selectors(self, toolkit, task_description, understanding):
        del toolkit, task_description, understanding
        raise AssertionError("selector generation should not run for SERP DOM fallback")


class _SelectorWorker(EnhancedWebWorker):
    async def _understand_page(self, toolkit, task_description):
        del toolkit, task_description
        return {
            "success": True,
            "page_type": "list",
            "main_function": "展示仓库列表",
            "key_fields": ["title", "url", "summary"],
            "semantic_snapshot": {},
        }

    async def _generate_selectors(self, toolkit, task_description, understanding):
        del toolkit, task_description, understanding
        return {
            "success": True,
            "item_selector": "article",
            "fields": {
                "title": "a",
                "link": "a/@href",
                "summary": "p",
            },
        }


def test_format_semantic_snapshot_for_llm_includes_main_text_blocks_and_stage():
    worker = EnhancedWebWorker(llm_client=_DummyLLM())

    text = asyncio.run(worker._format_semantic_snapshot_for_llm(
        {
            "page_type": "detail",
            "page_stage": "extracting",
            "url": "https://example.com/article",
            "title": "Example Article",
            "main_text": "This is the main article text.",
            "visible_text_blocks": [
                {"kind": "p", "text": "First paragraph", "selector": "article p:first-child"},
            ],
            "blocked_signals": ["body:captcha"],
        }
    ))

    assert "页面阶段: extracting" in text
    assert "主体文本: This is the main article text." in text
    assert "可见文本块:" in text
    assert "First paragraph" in text
    assert "阻塞信号: body:captcha" in text


def test_smart_extract_normalizes_attribute_style_fields():
    worker = _SelectorWorker(llm_client=_DummyLLM())
    result = asyncio.run(
        worker.smart_extract(
            toolkit=_FakeToolkit(),
            task_description="抓取前 1 个仓库的标题和链接",
            limit=1,
        )
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["data"] == [
        {
            "index": 1,
            "title": "OpenViking",
            "url": "https://github.com/volcengine/OpenViking",
            "link": "https://github.com/volcengine/OpenViking",
            "summary": "AI agent runtime",
        }
    ]


def test_extract_field_value_supports_attr_pseudo_selector():
    worker = _SelectorWorker(llm_client=_DummyLLM())
    payload = asyncio.run(
        worker._extract_field_value(
            _FakeItem(),
            "link",
            "a::attr(href)",
            "https://github.com/trending",
        )
    )

    assert payload == {"link": "https://github.com/volcengine/OpenViking"}


def test_post_process_results_filters_noise_and_deduplicates():
    worker = EnhancedWebWorker(llm_client=_DummyLLM())

    cleaned = worker._post_process_results(
        [
            {"index": 1, "title": "Home", "link": "https://example.com/"},
            {
                "index": 2,
                "title": "OpenViking",
                "link": "OpenViking",
                "title_url": "https://github.com/volcengine/OpenViking",
                "summary": "AI agent runtime",
            },
            {
                "index": 3,
                "title": "OpenViking",
                "link": "https://github.com/volcengine/OpenViking",
            },
        ],
        "抓取仓库标题和链接",
        {"page_type": "list", "key_fields": ["title", "url"]},
        10,
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


def test_smart_extract_prefers_semantic_snapshot_cards():
    worker = _SemanticOnlyWorker(llm_client=_DummyLLM())
    result = asyncio.run(
        worker.smart_extract(
            toolkit=object(),
            task_description="抓取前 2 条新闻的标题和链接",
            limit=2,
        )
    )

    assert result["success"] is True
    assert result["config"]["mode"] == "semantic_snapshot_cards"
    assert result["count"] == 2
    assert result["data"][0]["title"] == "OpenAI API 发布更新"
    assert result["data"][0]["url"] == "https://openai.com/index/openai-api/"
    assert result["data"][0]["link"] == "https://openai.com/index/openai-api/"
    assert result["data"][0]["source"] == "OpenAI"


def test_smart_extract_scans_beyond_first_limit_to_find_complete_items():
    worker = _SelectorWorker(llm_client=_DummyLLM())
    result = asyncio.run(
        worker.smart_extract(
            toolkit=_MixedToolkit(),
            task_description="抓取前 3 个仓库的标题和链接",
            limit=3,
        )
    )

    assert result["success"] is True
    assert result["count"] == 3
    assert [item["title"] for item in result["data"]] == [
        "OpenViking",
        "claude-plugins-official",
        "dimos",
    ]


def test_smart_extract_falls_back_to_serp_dom_cards_before_selectors():
    worker = _SerpDomWorker(llm_client=_DummyLLM())
    result = asyncio.run(
        worker.smart_extract(
            toolkit=_SerpDomToolkit(),
            task_description="抓取前 2 条结果的标题和链接",
            limit=2,
        )
    )

    assert result["success"] is True
    assert result["config"]["mode"] == "serp_dom_cards"
    assert result["count"] == 2
    assert [item["title"] for item in result["data"]] == [
        "API Platform | OpenAI",
        "OpenAI developer quickstart",
    ]
