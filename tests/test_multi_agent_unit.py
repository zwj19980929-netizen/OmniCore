"""
P3-2: 多 Agent 协作 — 单元测试

覆盖：
- TaskOutputType 枚举
- _extract_typed_output 类型化输出提取
- _resolve_task_params $ref 引用解析
- _evaluate_condition 条件表达式求值
- is_task_ready 条件任务检查
- dynamic_replan_node 动态任务插入
- Agent-to-Agent 消息类型
- OmniCoreState 新字段
"""
import pytest

from core.constants import TaskOutputType, TaskStatus, MAX_DYNAMIC_TASK_ADDITIONS
from core.state import OmniCoreState, create_initial_state, ensure_task_defaults
from core.task_executor import (
    _extract_typed_output,
    _resolve_task_params,
    _evaluate_condition,
    is_task_ready,
)
from core.message_bus import (
    MessageBus,
    AgentRequest,
    AgentResponse,
    MSG_AGENT_REQUEST,
    MSG_AGENT_RESPONSE,
    MSG_AGENT_STATUS,
)


# ---------------------------------------------------------------------------
# TaskOutputType
# ---------------------------------------------------------------------------

class TestTaskOutputType:
    def test_enum_values(self):
        assert str(TaskOutputType.FILE_DOWNLOAD) == "file_download"
        assert str(TaskOutputType.TEXT_EXTRACTION) == "text_extraction"
        assert str(TaskOutputType.COMMAND_OUTPUT) == "command_output"
        assert str(TaskOutputType.STRUCTURED_DATA) == "structured_data"
        assert str(TaskOutputType.SCREENSHOT) == "screenshot"
        assert str(TaskOutputType.ARTIFACT_REF) == "artifact_ref"


# ---------------------------------------------------------------------------
# _extract_typed_output
# ---------------------------------------------------------------------------

class TestExtractTypedOutput:
    def test_web_worker_extraction(self):
        task = {"tool_name": "web_worker", "task_type": "web_worker"}
        outcome = {"result": {"extracted_text": "hello world", "url": "https://example.com"}}
        output = _extract_typed_output(task, outcome)
        assert output["type"] == "text_extraction"
        assert output["content"] == "hello world"
        assert output["source_url"] == "https://example.com"

    def test_browser_agent_extraction(self):
        task = {"tool_name": "browser.navigate", "task_type": "browser_agent"}
        outcome = {"result": {"output": "page content", "url": "https://example.com"}}
        output = _extract_typed_output(task, outcome)
        assert output["type"] == "text_extraction"
        assert output["content"] == "page content"

    def test_file_worker_with_path(self):
        task = {"tool_name": "file.read_write", "task_type": "file_worker"}
        outcome = {"result": {"file_path": "/tmp/report.pdf", "file_size": 102400}}
        output = _extract_typed_output(task, outcome)
        assert output["type"] == "file_download"
        assert output["file_path"] == "/tmp/report.pdf"
        assert output["file_size"] == 102400

    def test_file_worker_with_content(self):
        task = {"tool_name": "file.read_write", "task_type": "file_worker"}
        outcome = {"result": {"content": "file text content"}}
        output = _extract_typed_output(task, outcome)
        assert output["type"] == "text_extraction"
        assert output["content"] == "file text content"

    def test_system_worker_extraction(self):
        task = {"tool_name": "system.execute", "task_type": "system_worker"}
        outcome = {"result": {"output": "command output", "returncode": 0}}
        output = _extract_typed_output(task, outcome)
        assert output["type"] == "command_output"
        assert output["stdout"] == "command output"
        assert output["returncode"] == 0

    def test_terminal_worker_extraction(self):
        task = {"tool_name": "terminal.execute", "task_type": "terminal_worker"}
        outcome = {"result": {"stdout": "ls output", "returncode": 0}}
        output = _extract_typed_output(task, outcome)
        assert output["type"] == "command_output"
        assert output["stdout"] == "ls output"

    def test_string_result(self):
        task = {"tool_name": "unknown", "task_type": "unknown"}
        outcome = {"result": "just a string"}
        output = _extract_typed_output(task, outcome)
        assert output["type"] == "text_extraction"
        assert output["content"] == "just a string"

    def test_none_result(self):
        task = {"tool_name": "unknown", "task_type": "unknown"}
        outcome = {"result": None}
        output = _extract_typed_output(task, outcome)
        assert output is None

    def test_empty_dict_result(self):
        task = {"tool_name": "unknown", "task_type": "unknown"}
        outcome = {"result": {}}
        output = _extract_typed_output(task, outcome)
        assert output is None


# ---------------------------------------------------------------------------
# _resolve_task_params
# ---------------------------------------------------------------------------

class TestResolveTaskParams:
    def test_basic_field_reference(self):
        task_outputs = {"task_1": {"file_path": "/tmp/report.zip", "type": "file_download"}}
        resolved = _resolve_task_params({"input": "$task_1.file_path"}, task_outputs)
        assert resolved["input"] == "/tmp/report.zip"

    def test_embedded_field_reference(self):
        task_outputs = {"task_2": {"file_path": "/tmp/report.html", "type": "file_download"}}
        resolved = _resolve_task_params({"task": "打开文件 $task_2.file_path"}, task_outputs)
        assert resolved["task"] == "打开文件 /tmp/report.html"

    def test_nested_reference_values(self):
        task_outputs = {"task_1": {"file_path": "/tmp/report.zip", "type": "file_download"}}
        resolved = _resolve_task_params(
            {"payload": {"path": "$task_1.file_path"}, "items": ["$task_1.type"]},
            task_outputs,
        )
        assert resolved["payload"]["path"] == "/tmp/report.zip"
        assert resolved["items"] == ["file_download"]

    def test_whole_output_reference(self):
        task_outputs = {"task_1": {"type": "text_extraction", "content": "hello"}}
        resolved = _resolve_task_params({"data": "$task_1"}, task_outputs)
        assert resolved["data"] == task_outputs["task_1"]

    def test_missing_reference_preserved(self):
        task_outputs = {"task_1": {"content": "hello"}}
        resolved = _resolve_task_params({"input": "$task_99.content"}, task_outputs)
        assert resolved["input"] == "$task_99.content"

    def test_missing_field_preserved(self):
        task_outputs = {"task_1": {"content": "hello"}}
        resolved = _resolve_task_params({"input": "$task_1.nonexistent"}, task_outputs)
        assert resolved["input"] == "$task_1.nonexistent"

    def test_non_ref_params_unchanged(self):
        task_outputs = {"task_1": {"content": "hello"}}
        resolved = _resolve_task_params(
            {"url": "https://example.com", "count": 5, "ref": "$task_1.content"},
            task_outputs,
        )
        assert resolved["url"] == "https://example.com"
        assert resolved["count"] == 5
        assert resolved["ref"] == "hello"

    def test_empty_task_outputs(self):
        resolved = _resolve_task_params({"input": "$task_1.content"}, {})
        assert resolved["input"] == "$task_1.content"


# ---------------------------------------------------------------------------
# _evaluate_condition
# ---------------------------------------------------------------------------

class TestEvaluateCondition:
    def test_ends_with(self):
        outputs = {"task_1": {"file_path": "/tmp/report.zip"}}
        assert _evaluate_condition("$task_1.file_path ends_with .zip", outputs) is True
        assert _evaluate_condition("$task_1.file_path ends_with .pdf", outputs) is False

    def test_starts_with(self):
        outputs = {"task_1": {"file_path": "/tmp/report.zip"}}
        assert _evaluate_condition("$task_1.file_path starts_with /tmp", outputs) is True
        assert _evaluate_condition("$task_1.file_path starts_with /home", outputs) is False

    def test_contains(self):
        outputs = {"task_1": {"content": "error: not found"}}
        assert _evaluate_condition("$task_1.content contains error", outputs) is True
        assert _evaluate_condition("$task_1.content contains success", outputs) is False

    def test_equals(self):
        outputs = {"task_1": {"returncode": "0"}}
        assert _evaluate_condition("$task_1.returncode == 0", outputs) is True
        assert _evaluate_condition("$task_1.returncode != 0", outputs) is False

    def test_exists(self):
        outputs = {"task_1": {"file_path": "/tmp/file.txt"}}
        assert _evaluate_condition("$task_1.file_path exists", outputs) is True
        outputs_empty = {"task_1": {"file_path": ""}}
        assert _evaluate_condition("$task_1.file_path exists", outputs_empty) is False

    def test_missing_task_returns_false_for_exists(self):
        assert _evaluate_condition("$task_99.file_path exists", {}) is False

    def test_empty_expression_returns_true(self):
        assert _evaluate_condition("", {}) is True
        assert _evaluate_condition("  ", {}) is True

    def test_unknown_operator_returns_true(self):
        outputs = {"task_1": {"value": "abc"}}
        assert _evaluate_condition("$task_1.value unknown_op xyz", outputs) is True


# ---------------------------------------------------------------------------
# is_task_ready with conditional
# ---------------------------------------------------------------------------

class TestIsTaskReadyConditional:
    def _make_queue(self, statuses):
        """Helper: create a task queue with given task_id → status mapping."""
        return [
            {"task_id": tid, "status": status, "params": {}}
            for tid, status in statuses.items()
        ]

    def test_basic_dependency(self):
        queue = self._make_queue({"t1": "completed", "t2": "pending"})
        task = {"task_id": "t2", "depends_on": ["t1"], "params": {}, "status": "pending"}
        assert is_task_ready(task, queue) is True

    def test_unmet_dependency(self):
        queue = self._make_queue({"t1": "running", "t2": "pending"})
        task = {"task_id": "t2", "depends_on": ["t1"], "params": {}, "status": "pending"}
        assert is_task_ready(task, queue) is False

    def test_conditional_met(self):
        queue = self._make_queue({"t1": "completed", "t2": "pending"})
        task_outputs = {"t1": {"file_path": "/tmp/report.zip"}}
        task = {
            "task_id": "t2", "depends_on": ["t1"], "params": {},
            "status": "pending",
            "conditional": {"when": "$t1.file_path ends_with .zip", "else_skip": True},
        }
        assert is_task_ready(task, queue, task_outputs) is True

    def test_conditional_not_met_skip(self):
        queue = self._make_queue({"t1": "completed", "t2": "pending"})
        task_outputs = {"t1": {"file_path": "/tmp/report.pdf"}}
        task = {
            "task_id": "t2", "depends_on": ["t1"], "params": {},
            "status": "pending",
            "conditional": {"when": "$t1.file_path ends_with .zip", "else_skip": True},
        }
        assert is_task_ready(task, queue, task_outputs) is False
        # else_skip=True 应将任务标记为 completed
        assert task["status"] == str(TaskStatus.COMPLETED)
        assert task["result"]["skipped"] is True

    def test_conditional_not_met_no_skip(self):
        queue = self._make_queue({"t1": "completed", "t2": "pending"})
        task_outputs = {"t1": {"file_path": "/tmp/report.pdf"}}
        task = {
            "task_id": "t2", "depends_on": ["t1"], "params": {},
            "status": "pending",
            "conditional": {"when": "$t1.file_path ends_with .zip", "else_skip": False},
        }
        assert is_task_ready(task, queue, task_outputs) is False
        # else_skip=False 不自动跳过
        assert task["status"] == "pending"

    def test_no_task_outputs_skips_conditional(self):
        """task_outputs=None 时不检查 conditional（向后兼容）。"""
        queue = self._make_queue({"t1": "completed"})
        task = {
            "task_id": "t2", "depends_on": ["t1"], "params": {},
            "status": "pending",
            "conditional": {"when": "$t1.file_path ends_with .zip", "else_skip": True},
        }
        assert is_task_ready(task, queue, None) is True


# ---------------------------------------------------------------------------
# Agent-to-Agent Messages
# ---------------------------------------------------------------------------

class TestAgentMessages:
    def test_message_types_exist(self):
        assert MSG_AGENT_REQUEST == "agent_request"
        assert MSG_AGENT_RESPONSE == "agent_response"
        assert MSG_AGENT_STATUS == "agent_status"

    def test_agent_request_roundtrip(self):
        req = AgentRequest(
            from_agent="browser_agent",
            to_agent="file_worker",
            task={"task_id": "sub_1", "description": "save file"},
            callback_task_id="task_1",
        )
        payload = req.to_payload()
        restored = AgentRequest.from_payload(payload)
        assert restored.from_agent == "browser_agent"
        assert restored.to_agent == "file_worker"
        assert restored.task["task_id"] == "sub_1"
        assert restored.callback_task_id == "task_1"

    def test_agent_response_roundtrip(self):
        resp = AgentResponse(
            from_agent="file_worker",
            to_agent="browser_agent",
            request_task_id="sub_1",
            result={"file_path": "/tmp/saved.txt"},
            success=True,
        )
        payload = resp.to_payload()
        restored = AgentResponse.from_payload(payload)
        assert restored.success is True
        assert restored.result["file_path"] == "/tmp/saved.txt"

    def test_publish_agent_request_on_bus(self):
        bus = MessageBus()
        req = AgentRequest(
            from_agent="planner",
            to_agent="executor",
            task={"task_id": "t1", "description": "do something"},
        )
        bus.publish("planner", "executor", MSG_AGENT_REQUEST, req.to_payload())
        msgs = bus.query(message_type=MSG_AGENT_REQUEST, target="executor")
        assert len(msgs) == 1
        assert msgs[0].payload["from_agent"] == "planner"

    def test_publish_agent_response_on_bus(self):
        bus = MessageBus()
        resp = AgentResponse(
            from_agent="executor",
            to_agent="planner",
            request_task_id="t1",
            result={"output": "done"},
            success=True,
        )
        bus.publish("executor", "planner", MSG_AGENT_RESPONSE, resp.to_payload())
        msgs = bus.query(message_type=MSG_AGENT_RESPONSE, target="planner")
        assert len(msgs) == 1
        restored = AgentResponse.from_payload(msgs[0].payload)
        assert restored.success is True


# ---------------------------------------------------------------------------
# OmniCoreState 新字段
# ---------------------------------------------------------------------------

class TestStateNewFields:
    def test_create_initial_state_has_new_fields(self):
        state = create_initial_state("test input")
        assert state["task_outputs"] == {}
        assert state["dynamic_task_additions"] == []
        assert state["agent_messages"] == []

    def test_task_outputs_write_read(self):
        state = create_initial_state("test")
        state["task_outputs"]["task_1"] = {
            "type": "file_download",
            "file_path": "/tmp/report.pdf",
        }
        assert state["task_outputs"]["task_1"]["file_path"] == "/tmp/report.pdf"


# ---------------------------------------------------------------------------
# dynamic_replan_node
# ---------------------------------------------------------------------------

class TestDynamicReplanNode:
    def test_no_additions_noop(self):
        from core.graph import dynamic_replan_node

        state = create_initial_state("test")
        state["task_queue"] = [
            ensure_task_defaults({
                "task_id": "t1", "task_type": "web_worker",
                "description": "test", "params": {},
                "status": "completed", "result": {}, "priority": 5,
            })
        ]
        result = dynamic_replan_node(state)
        assert len(result["task_queue"]) == 1

    def test_inserts_new_tasks(self):
        from core.graph import dynamic_replan_node

        state = create_initial_state("test")
        state["task_queue"] = [
            ensure_task_defaults({
                "task_id": "t1", "task_type": "web_worker",
                "description": "test", "params": {},
                "status": "completed", "result": {}, "priority": 5,
            })
        ]
        state["dynamic_task_additions"] = [
            {
                "task_id": "t2", "task_type": "file_worker",
                "description": "save result", "params": {},
                "status": "pending", "result": None, "priority": 5,
            }
        ]
        result = dynamic_replan_node(state)
        assert len(result["task_queue"]) == 2
        assert result["task_queue"][1]["task_id"] == "t2"
        assert result["dynamic_task_additions"] == []

    def test_skips_duplicate_task_ids(self):
        from core.graph import dynamic_replan_node

        state = create_initial_state("test")
        state["task_queue"] = [
            ensure_task_defaults({
                "task_id": "t1", "task_type": "web_worker",
                "description": "test", "params": {},
                "status": "completed", "result": {}, "priority": 5,
            })
        ]
        state["dynamic_task_additions"] = [
            {
                "task_id": "t1", "task_type": "file_worker",
                "description": "duplicate", "params": {},
                "status": "pending", "result": None, "priority": 5,
            }
        ]
        result = dynamic_replan_node(state)
        assert len(result["task_queue"]) == 1  # 不新增

    def test_respects_max_additions_limit(self):
        from core.graph import dynamic_replan_node

        state = create_initial_state("test")
        state["task_queue"] = []
        # 创建超过限制的任务
        state["dynamic_task_additions"] = [
            {
                "task_id": f"t_{i}", "task_type": "web_worker",
                "description": f"task {i}", "params": {},
                "status": "pending", "result": None, "priority": 5,
            }
            for i in range(MAX_DYNAMIC_TASK_ADDITIONS + 5)
        ]
        result = dynamic_replan_node(state)
        assert len(result["task_queue"]) == MAX_DYNAMIC_TASK_ADDITIONS

    def test_skips_self_referencing_dependency(self):
        from core.graph import dynamic_replan_node

        state = create_initial_state("test")
        state["task_queue"] = []
        state["dynamic_task_additions"] = [
            {
                "task_id": "t1", "task_type": "web_worker",
                "description": "self ref", "params": {},
                "status": "pending", "result": None, "priority": 5,
                "depends_on": ["t1"],  # 自引用
            }
        ]
        result = dynamic_replan_node(state)
        assert len(result["task_queue"]) == 0  # 被跳过
