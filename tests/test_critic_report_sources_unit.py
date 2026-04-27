from agents.critic import CriticAgent
from core.message_bus import MSG_TIME_CONTEXT, MessageBus
from core.state import create_initial_state


class _UnusedLLM:
    def chat_with_system(self, **_kwargs):
        raise AssertionError("deterministic report review should run before LLM review")

    def parse_json_response(self, _response):
        return {}


def test_critic_rejects_source_sensitive_report_without_clickable_urls(tmp_path):
    report_path = tmp_path / "report.html"
    report_path.write_text("<h1>OpenAI 最新模型</h1><p>来源：OpenAI 官方博客。</p>", encoding="utf-8")

    critic = CriticAgent(llm_client=_UnusedLLM())
    result = critic.review_task_result(
        "生成中文HTML报告，总结OpenAI最新模型，并给出信息源",
        {"success": True, "file_path": str(report_path)},
    )

    assert result["approved"] is False
    assert "来源链接" in result["issues"][0]


def test_critic_rejects_obviously_stale_latest_report(tmp_path):
    report_path = tmp_path / "report.html"
    report_path.write_text(
        "<h1>OpenAI 最新模型</h1>"
        "<p>报告生成日期：2025 年 1 月 17 日</p>"
        "<a href='https://openai.com/index/introducing-gpt-5-2/'>source</a>",
        encoding="utf-8",
    )

    critic = CriticAgent(llm_client=_UnusedLLM())
    result = critic.review_task_result(
        "生成中文HTML报告，总结OpenAI最新模型，并给出信息源",
        {"success": True, "file_path": str(report_path)},
    )

    assert result["approved"] is False
    assert "过期" in result["summary"]


def test_critic_rejects_time_sensitive_report_without_generated_date(tmp_path):
    report_path = tmp_path / "report.html"
    report_path.write_text(
        "<h1>OpenAI 最新模型</h1>"
        "<a href='https://openai.com/index/introducing-gpt-5-2/'>source</a>",
        encoding="utf-8",
    )

    critic = CriticAgent(llm_client=_UnusedLLM())
    result = critic.review_task_result(
        "生成中文HTML报告，总结OpenAI最新模型，并给出信息源",
        {"success": True, "file_path": str(report_path)},
        current_time_context={"local_date": "2026-04-24", "current_year": 2026},
    )

    assert result["approved"] is False
    assert "日期基准" in result["summary"]


def test_critic_rejects_stale_latest_claim_even_with_current_report_date(tmp_path):
    report_path = tmp_path / "report.html"
    report_path.write_text(
        "<h1>OpenAI 最新模型</h1>"
        "<p>报告生成日期：2026-04-24</p>"
        "<p>2025 年初最新模型仍然是主要结论。</p>"
        "<a href='https://openai.com/index/introducing-gpt-5-2/'>source</a>",
        encoding="utf-8",
    )

    critic = CriticAgent(llm_client=_UnusedLLM())
    result = critic.review_task_result(
        "生成中文HTML报告，总结OpenAI最新模型，并给出信息源",
        {"success": True, "file_path": str(report_path)},
        current_time_context={"local_date": "2026-04-24", "current_year": 2026},
    )

    assert result["approved"] is False
    assert "过期" in result["summary"]


def test_critic_does_not_apply_time_rule_to_non_time_sensitive_report(tmp_path):
    report_path = tmp_path / "report.html"
    report_path.write_text(
        "<h1>资料报告</h1>"
        "<p>这里是一份普通资料汇总。</p>"
        "<a href='https://example.com/source'>source</a>",
        encoding="utf-8",
    )

    critic = CriticAgent(llm_client=_UnusedLLM())
    result = critic._review_generated_report_sources(
        "生成资料报告并给出来源",
        {"success": True, "file_path": str(report_path)},
        current_time_context={"local_date": "2026-04-24", "current_year": 2026},
    )

    assert result is None


def test_critic_review_uses_message_bus_time_context(tmp_path):
    report_path = tmp_path / "report.html"
    report_path.write_text(
        "<h1>OpenAI 最新模型</h1>"
        "<p>报告生成日期：2025 年 1 月 17 日</p>"
        "<a href='https://openai.com/index/introducing-gpt-5-2/'>source</a>",
        encoding="utf-8",
    )
    state = create_initial_state("生成OpenAI最新模型报告")
    bus = MessageBus.from_dict(state.get("message_bus", []))
    bus.publish(
        "system",
        "*",
        MSG_TIME_CONTEXT,
        {"value": {"local_date": "2026-04-24", "current_year": 2026}},
    )
    state["message_bus"] = bus.to_dict()
    state["task_queue"] = [
        {
            "task_id": "task_1",
            "task_type": "file_worker",
            "description": "生成中文HTML报告，总结OpenAI最新模型，并给出信息源",
            "status": "completed",
            "result": {"success": True, "file_path": str(report_path)},
        }
    ]

    reviewed = CriticAgent(llm_client=_UnusedLLM()).review(state)

    assert reviewed["critic_approved"] is False
    assert reviewed["task_queue"][0]["critic_review"]["summary"] == "报告明显过期"


def test_critic_rejects_search_homepage_navigation_links():
    critic = CriticAgent(llm_client=_UnusedLLM())
    result = critic.review_task_result(
        "Use a browser to visit Baidu search (https://www.baidu.com), "
        "search Baidu for 'AI 大模型 最新动态 2026年4月 DeepSeek OpenAI' "
        "and extract at least 5 search results (title, URL, snippet).",
        {
            "success": True,
            "data": [
                {"title": "hao123", "link": "https://www.hao123.com/?src=from_pc"},
                {"title": "搭子DuMate", "link": "https://cloud.baidu.com/product/dumate.html?track=bdsy"},
                {"title": "关于百度", "link": "https://home.baidu.com/"},
                {"title": "About Baidu", "link": "http://ir.baidu.com/"},
                {"title": "帮助中心", "link": "https://help.baidu.com/question?prod_id=1"},
            ],
        },
    )

    assert result["approved"] is False
    assert result["summary"] == "列表抽取结果与任务主题不匹配"


def test_critic_parses_at_least_search_result_count():
    assert CriticAgent._extract_target_count("extract at least 5 search results") == 5
