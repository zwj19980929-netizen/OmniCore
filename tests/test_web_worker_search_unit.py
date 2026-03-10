import asyncio
import base64

from agents.web_worker import WebWorker


def test_decode_bing_redirect_url_with_base64_payload():
    target = "https://www.reuters.com/world/middle-east/"
    encoded = base64.urlsafe_b64encode(target.encode("utf-8")).decode("ascii").rstrip("=")
    redirect = f"https://www.bing.com/ck/a?u=a1{encoded}"
    decoded = WebWorker._decode_redirect_url(redirect)
    assert decoded == target


def test_search_engine_domain_filter():
    assert WebWorker._is_search_engine_domain("https://www.bing.com/search?q=test") is True
    assert WebWorker._is_search_engine_domain("https://www.reuters.com/world/") is False


class _PlannerLLM:
    def __init__(self, payload):
        self.payload = payload

    def chat_with_system(self, **_kwargs):
        return {"ok": True}

    def parse_json_response(self, _response):
        return self.payload


def test_plan_search_queries_uses_llm_planned_short_queries():
    worker = WebWorker(llm_client=_PlannerLLM({"queries": ["US Iran strikes Reuters", "site:bbc.com US Iran conflict"]}))

    queries = worker.plan_search_queries(
        "定向检索 Reuters 与 BBC 关于近期美国与伊朗行动的直接报道，并核实 war wording",
        base_query="recent US Iran conflict news authoritative sources",
    )

    assert queries == ["US Iran strikes Reuters", "site:bbc.com US Iran conflict"]


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


def test_search_for_result_cards_falls_back_when_bing_is_blank(monkeypatch):
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
            if "google.com" in self.current_url:
                return type("Result", (), {"success": True})()
            return type("Result", (), {"success": False})()

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

    assert any("bing.com/search" in url for url in visited)
    assert any("google.com/search" in url for url in visited)
    assert cards[0]["link"] == "https://www.reuters.com/world/article-123"
