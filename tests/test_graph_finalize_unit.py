import ast
import time
from collections import Counter
from pathlib import Path

from core.graph import _repair_mojibake_text, finalize_node
import core.graph as graph_module


def _build_bus_data(entries=None):
    """Build a serialized MessageBus list for test state dicts."""
    if not entries:
        return []
    now = time.time()
    return [
        {"source": src, "target": tgt, "message_type": mtype,
         "payload": payload, "timestamp": now, "job_id": ""}
        for src, tgt, mtype, payload in entries
    ]


def test_finalize_node_builds_delivery_summary_with_artifacts(monkeypatch):
    class _FailingLLM:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("LLM unavailable")

    monkeypatch.setattr(graph_module, "LLMClient", _FailingLLM)

    state = {
        "messages": [],
        "user_input": "summarize and save the results",
        "session_id": "session_1",
        "job_id": "job_1",
        "current_intent": "research",
        "intent_confidence": 1.0,
        "task_queue": [
            {
                "task_id": "task_1",
                "task_type": "file_worker",
                "tool_name": "file.read_write",
                "description": "Write the report to disk",
                "params": {"file_path": "D:/tmp/report.txt"},
                "status": "completed",
                "result": {"success": True, "file_path": "D:/tmp/report.txt"},
                "priority": 10,
            },
            {
                "task_id": "task_2",
                "task_type": "web_worker",
                "tool_name": "web.fetch_and_extract",
                "description": "Fetch additional context",
                "params": {},
                "status": "failed",
                "result": {"success": False, "error": "Request timed out"},
                "priority": 5,
            },
        ],
        "current_task_index": 0,
        "message_bus": [],
        "artifacts": [],
        "critic_feedback": "One source could not be fetched.",
        "critic_approved": False,
        "human_approved": True,
        "needs_human_confirm": False,
        "error_trace": "",
        "final_output": "",
        "delivery_package": {},
        "execution_status": "reviewing",
        "replan_count": 0,
        "validator_passed": True,
    }

    result = finalize_node(state)

    assert "Completed 1 of 2 task(s)" in result["final_output"]
    assert "Completed work:" in result["final_output"]
    assert "Deliverables:" in result["final_output"]
    assert "report.txt" in result["final_output"]
    assert "Open issues:" in result["final_output"]
    assert "Request timed out" in result["final_output"]
    assert result["delivery_package"]["completed_task_count"] == 1
    assert result["delivery_package"]["issues"][0]["error"] == "Request timed out"
    assert result["execution_status"] == "completed_with_issues"
    assert result["artifacts"][0]["path"] == "D:/tmp/report.txt"


def test_finalize_node_includes_parsed_findings_from_structured_data(monkeypatch):
    class _FailingLLM:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("LLM unavailable")

    monkeypatch.setattr(graph_module, "LLMClient", _FailingLLM)

    state = {
        "messages": [],
        "user_input": "What happened in US-Iran today?",
        "session_id": "session_findings",
        "job_id": "job_findings",
        "current_intent": "information_query",
        "intent_confidence": 1.0,
        "task_queue": [
            {
                "task_id": "task_1",
                "task_type": "web_worker",
                "tool_name": "web.fetch_and_extract",
                "description": "Fetch latest US-Iran updates",
                "params": {"limit": 10},
                "status": "completed",
                "result": {
                    "success": True,
                    "source": "https://example.com/news",
                    "data": [
                        {
                            "title": "US-Iran maritime tensions rise in Gulf",
                            "date": "2026-03-05",
                            "link": "https://example.com/news/1",
                        },
                        {
                            "title": "Diplomatic channel reopens for de-escalation",
                            "date": "2026-03-05",
                            "link": "https://example.com/news/2",
                        },
                    ],
                },
                "priority": 10,
            }
        ],
        "current_task_index": 0,
        "message_bus": [],
        "artifacts": [],
        "critic_feedback": "All tasks approved",
        "critic_approved": True,
        "human_approved": True,
        "needs_human_confirm": False,
        "error_trace": "",
        "final_output": "",
        "delivery_package": {},
        "execution_status": "reviewing",
        "replan_count": 0,
        "validator_passed": True,
    }

    result = finalize_node(state)

    assert "Findings:" in result["final_output"]
    assert "US-Iran maritime tensions rise in Gulf" in result["final_output"]
    assert "https://example.com/news/1" in result["final_output"]


def test_finalize_node_prefers_user_facing_answer_for_completed_tasks(monkeypatch):
    class _FakeResponse:
        def __init__(self, content):
            self.content = content

    class _FakeLLM:
        def __init__(self, *args, **kwargs):
            pass

        def chat_with_system(self, *args, **kwargs):
            return _FakeResponse(
                "根据当前抓取到的最新信息，美伊相关局势仍在升级，既有军事施压，也出现了重新接触和降温信号。"
            )

    monkeypatch.setattr(graph_module, "LLMClient", _FakeLLM)

    state = {
        "messages": [],
        "user_input": "我想知道最近美伊局势怎么样了？",
        "session_id": "session_synth",
        "job_id": "job_synth",
        "current_intent": "information_query",
        "intent_confidence": 1.0,
        "task_queue": [
            {
                "task_id": "task_1",
                "task_type": "web_worker",
                "tool_name": "web.fetch_and_extract",
                "description": "Fetch latest US-Iran updates",
                "params": {"limit": 10},
                "status": "completed",
                "result": {
                    "success": True,
                    "data": [
                        {
                            "title": "US-Iran maritime tensions rise in Gulf",
                            "date": "2026-03-05",
                            "link": "https://example.com/news/1",
                        }
                    ],
                },
                "priority": 10,
            }
        ],
        "current_task_index": 0,
        "message_bus": [],
        "artifacts": [],
        "critic_feedback": "All tasks approved",
        "critic_approved": True,
        "human_approved": True,
        "needs_human_confirm": False,
        "error_trace": "",
        "final_output": "",
        "delivery_package": {},
        "execution_status": "reviewing",
        "replan_count": 0,
        "validator_passed": True,
    }

    result = finalize_node(state)

    assert result["execution_status"] == "completed"
    assert result["final_output"].startswith("根据当前抓取到的最新信息")
    assert result["delivery_package"]["completed_task_count"] == 1


def test_finalize_node_uses_deterministic_table_for_explicit_list_requests(monkeypatch):
    class _ShouldNotCallLLM:
        def __init__(self, *args, **kwargs):
            raise AssertionError("LLMClient should not be called for explicit structured list output")

    monkeypatch.setattr(graph_module, "LLMClient", _ShouldNotCallLLM)

    state = {
        "messages": [],
        "user_input": "去 https://huggingface.co/models 抓取前 3 个模型的名称和链接",
        "session_id": "session_list",
        "job_id": "job_list",
        "current_intent": "web_scraping",
        "intent_confidence": 1.0,
        "task_queue": [
            {
                "task_id": "task_1",
                "task_type": "web_worker",
                "tool_name": "web.fetch_and_extract",
                "description": "从 Hugging Face 模型页面抓取前 3 个模型的名称和链接",
                "params": {"limit": 3},
                "status": "completed",
                "result": {
                    "success": True,
                    "data": [
                        {"title": "Model A", "url": "https://example.com/a"},
                        {"name": "Model B", "link": "https://example.com/b"},
                        {"title": "Model C", "url": "https://example.com/c"},
                    ],
                },
                "priority": 10,
            }
        ],
        "current_task_index": 0,
        "message_bus": [],
        "artifacts": [],
        "critic_feedback": "",
        "critic_approved": True,
        "human_approved": True,
        "needs_human_confirm": False,
        "error_trace": "",
        "final_output": "",
        "delivery_package": {},
        "execution_status": "reviewing",
        "replan_count": 0,
        "validator_passed": True,
    }

    result = finalize_node(state)

    assert result["execution_status"] == "completed"
    assert "| 序号 | 标题 | 链接 |" in result["final_output"]
    assert "| 1 | Model A | https://example.com/a |" in result["final_output"]
    assert "| 2 | Model B | https://example.com/b |" in result["final_output"]
    assert "| 3 | Model C | https://example.com/c |" in result["final_output"]


def test_core_graph_has_no_duplicate_critical_top_level_functions():
    tree = ast.parse(Path("core/graph.py").read_text(encoding="utf-8"))
    counts = Counter(
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    )

    duplicates = {name: count for name, count in counts.items() if count > 1}
    assert duplicates == {}


def test_repair_mojibake_text_roundtrip_utf8_latin1():
    original = "\u65e5\u672c\u7d27\u6025\u8bc4\u4f30"
    mojibake = original.encode("utf-8").decode("latin-1")
    assert _repair_mojibake_text(mojibake) == original


def test_finalize_node_marks_waiting_for_approval_state(monkeypatch):
    class _ShouldNotCallLLM:
        def __init__(self, *args, **kwargs):
            raise AssertionError("LLMClient should not be called for waiting states")

    monkeypatch.setattr(graph_module, "LLMClient", _ShouldNotCallLLM)

    state = {
        "messages": [],
        "user_input": "send the prepared webhook",
        "session_id": "session_2",
        "job_id": "job_2",
        "current_intent": "integration",
        "intent_confidence": 1.0,
        "task_queue": [
            {
                "task_id": "task_api",
                "task_type": "api_worker",
                "tool_name": "api.call",
                "description": "Send webhook",
                "params": {"method": "POST", "url": "https://example.com"},
                "status": "waiting_for_approval",
                "result": {"approval_required": True},
                "priority": 10,
            }
        ],
        "current_task_index": 0,
        "message_bus": [],
        "artifacts": [],
        "critic_feedback": "",
        "critic_approved": True,
        "human_approved": True,
        "needs_human_confirm": False,
        "error_trace": "",
        "final_output": "",
        "delivery_package": {},
        "execution_status": "reviewing",
        "replan_count": 0,
        "validator_passed": True,
    }

    result = finalize_node(state)

    assert result["execution_status"] == "waiting_for_approval"
    assert "waiting for approval" in result["final_output"].lower()
    assert result["delivery_package"]["recommended_next_step"]


def test_finalize_node_refuses_fact_answer_without_direct_answer_or_evidence(monkeypatch):
    class _ShouldNotCallLLM:
        def __init__(self, *args, **kwargs):
            raise AssertionError("LLMClient should not be called for unsupported no-task fallback")

    monkeypatch.setattr(graph_module, "LLMClient", _ShouldNotCallLLM)

    state = {
        "messages": [type("Msg", (), {"content": "Router 分析完成: 解析失败: malformed json"})()],
        "user_input": "你把你获取到的信息给我看看",
        "session_id": "session_no_evidence",
        "job_id": "job_no_evidence",
        "current_intent": "unknown",
        "intent_confidence": 0.0,
        "task_queue": [],
        "current_task_index": 0,
        "message_bus": [],
        "artifacts": [],
        "critic_feedback": "",
        "critic_approved": False,
        "human_approved": True,
        "needs_human_confirm": False,
        "error_trace": "",
        "final_output": "",
        "delivery_package": {},
        "execution_status": "reviewing",
        "replan_count": 0,
        "validator_passed": True,
    }

    result = finalize_node(state)

    assert result["execution_status"] == "completed_with_issues"
    assert result["critic_approved"] is False
    assert "System note:" in result["final_output"]
    assert result["delivery_package"]["review_status"] == "needs_attention"


def test_finalize_node_prefers_router_direct_answer_without_llm(monkeypatch):
    class _ShouldNotCallLLM:
        def __init__(self, *args, **kwargs):
            raise AssertionError("LLMClient should not be called when router_direct_answer exists")

    monkeypatch.setattr(graph_module, "LLMClient", _ShouldNotCallLLM)

    state = {
        "messages": [],
        "user_input": "现在的时间是多少？",
        "session_id": "session_3",
        "job_id": "job_3",
        "current_intent": "information_query",
        "intent_confidence": 1.0,
        "task_queue": [],
        "current_task_index": 0,
        "message_bus": _build_bus_data([
            ("router", "finalize", "direct_answer", {"value": "当前时间是 2026-03-05 星期四 10:17:54（CST）。"}),
            ("system", "*", "time_context", {"value": {"iso_datetime": "2026-03-05T10:17:54+08:00", "local_date": "2026-03-05", "local_time": "10:17:54", "weekday": "Thursday", "timezone": "CST"}}),
        ]),
        "artifacts": [],
        "critic_feedback": "",
        "critic_approved": False,
        "human_approved": True,
        "needs_human_confirm": False,
        "error_trace": "",
        "final_output": "",
        "delivery_package": {},
        "execution_status": "reviewing",
        "replan_count": 0,
        "validator_passed": True,
    }

    result = finalize_node(state)

    assert result["execution_status"] == "completed"
    assert result["critic_approved"] is True
    assert result["final_output"] == "当前时间是 2026-03-05 星期四 10:17:54（CST）。"


def test_finalize_node_passes_replan_answer_guidance_to_synthesizer(monkeypatch):
    class _CapturingLLM:
        last_user_message = ""

        def __init__(self, *args, **kwargs):
            pass

        def chat_with_system(self, *, user_message, **_kwargs):
            type(self).last_user_message = user_message
            return type("Resp", (), {"content": "Based on the verified timeline, it lasted 6 days."})()

    monkeypatch.setattr(graph_module, "LLMClient", _CapturingLLM)

    state = {
        "messages": [],
        "user_input": "最近美伊冲突持续了几天？",
        "session_id": "session_guidance",
        "job_id": "job_guidance",
        "current_intent": "information_query",
        "intent_confidence": 1.0,
        "task_queue": [
            {
                "task_id": "task_1",
                "task_type": "web_worker",
                "tool_name": "web.fetch_and_extract",
                "description": "Fetch authoritative conflict timeline",
                "params": {"url": "https://example.com/report"},
                "status": "completed",
                "result": {
                    "success": True,
                    "data": [
                        {
                            "title": "Conflict timeline",
                            "summary": "Started on 2026-03-01 and latest report is 2026-03-06.",
                            "link": "https://example.com/report",
                        }
                    ],
                },
                "priority": 10,
            }
        ],
        "current_task_index": 0,
        "message_bus": _build_bus_data([
            ("critic", "finalize", "final_instructions", {"value": [
                "Based on the extracted dates, calculate duration and answer the user directly."
            ]}),
        ]),
        "artifacts": [],
        "critic_feedback": "",
        "critic_approved": True,
        "human_approved": True,
        "needs_human_confirm": False,
        "error_trace": "",
        "final_output": "",
        "delivery_package": {},
        "execution_status": "reviewing",
        "replan_count": 0,
        "validator_passed": True,
    }

    result = finalize_node(state)

    assert result["execution_status"] == "completed"
    assert result["final_output"] == "Based on the verified timeline, it lasted 6 days."
    assert "Answer guidance:" in _CapturingLLM.last_user_message
    assert "calculate duration and answer the user directly" in _CapturingLLM.last_user_message
