"""
S4: Tool Pipeline 单元测试

覆盖：
- Pipeline 全阶段正常流程
- 各阶段失败（strict vs 降级模式）
- Schema 校验（正确参数、缺失字段、类型错误）
- 语义校验（危险路径拒绝、正常路径通过、自定义 validate_input）
- 上下文注入（路径展开、required_context 注入）
- 权限检查（allow / ask）
- 结果规范化（各种 raw_result → ToolResult）
- ToolResult.to_outcome_dict 兼容性
"""
import asyncio
import os
import copy
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.tool_pipeline import (
    ToolPipeline,
    ToolPipelineStage,
    ToolExecutionContext,
    ToolResult,
    StageError,
    _validate_file_paths,
    get_tool_pipeline,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spec(
    name="test.tool",
    input_schema=None,
    validate_input=None,
    required_context=None,
    concurrent_safe=True,
    destructive=False,
    risk_level="low",
    task_type="test_worker",
    output_type="text_extraction",
):
    """创建 mock ToolSpec。"""
    spec = MagicMock()
    spec.name = name
    spec.task_type = task_type
    spec.input_schema = input_schema or {}
    spec.validate_input = validate_input
    spec.required_context = required_context or []
    spec.concurrent_safe = concurrent_safe
    spec.destructive = destructive
    spec.risk_level = risk_level
    spec.output_type = output_type
    return spec


def _make_registered_tool(spec=None, adapter_name="test_adapter"):
    tool = MagicMock()
    tool.spec = spec or _make_spec()
    tool.adapter_name = adapter_name
    return tool


def _make_state(**kwargs):
    state = {
        "session_id": "test-session-001",
        "job_id": "test-job-001",
        "task_queue": [],
        "current_task_id": "task_1",
    }
    state.update(kwargs)
    return state


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# ToolResult 测试
# ---------------------------------------------------------------------------

class TestToolResult:
    def test_success_result(self):
        r = ToolResult(success=True, output="hello")
        assert r.success is True
        assert r.error is None

    def test_failure_result(self):
        r = ToolResult(success=False, error="boom", error_type="RuntimeError")
        assert r.success is False
        assert r.error == "boom"

    def test_to_outcome_dict_success(self):
        r = ToolResult(
            success=True,
            output="result text",
            structured_data={"success": True, "content": "abc"},
        )
        outcome = r.to_outcome_dict(
            task_type="web_worker",
            tool_name="web.fetch",
            risk_level="low",
        )
        assert outcome["status"] == "completed"
        assert outcome["result"]["success"] is True
        assert outcome["result"]["content"] == "abc"
        assert outcome["error_trace"] == ""
        assert outcome["failure_type"] is None

    def test_to_outcome_dict_failure(self):
        r = ToolResult(
            success=False,
            error="network timeout",
            error_type="TimeoutError",
        )
        outcome = r.to_outcome_dict(task_type="web_worker", tool_name="web.fetch")
        assert outcome["status"] == "failed"
        assert outcome["failure_type"] == "TimeoutError"
        assert outcome["error_trace"] == "network timeout"

    def test_to_outcome_dict_output_fallback(self):
        r = ToolResult(success=True, output="hello world")
        outcome = r.to_outcome_dict()
        assert outcome["result"]["output"] == "hello world"
        assert outcome["result"]["success"] is True


# ---------------------------------------------------------------------------
# StageError / ToolExecutionContext 测试
# ---------------------------------------------------------------------------

class TestToolExecutionContext:
    def test_has_fatal_error(self):
        ctx = ToolExecutionContext(tool_name="t", raw_params={})
        assert ctx.has_fatal_error is False
        ctx.stage_errors.append(StageError(ToolPipelineStage.SCHEMA_VALIDATE, "err", fatal=False))
        assert ctx.has_fatal_error is False
        ctx.stage_errors.append(StageError(ToolPipelineStage.SEMANTIC_VALIDATE, "err", fatal=True))
        assert ctx.has_fatal_error is True

    def test_effective_params_priority(self):
        ctx = ToolExecutionContext(tool_name="t", raw_params={"a": 1})
        assert ctx.effective_params == {"a": 1}
        ctx.validated_params = {"a": 1, "b": 2}
        assert ctx.effective_params == {"a": 1, "b": 2}
        ctx.injected_params = {"a": 1, "b": 2, "c": 3}
        assert ctx.effective_params == {"a": 1, "b": 2, "c": 3}


# ---------------------------------------------------------------------------
# Schema 校验测试
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def test_no_schema_passes(self):
        pipeline = ToolPipeline(strict_mode=True)
        ctx = ToolExecutionContext(tool_name="t", raw_params={"x": 1})
        pipeline._stage_schema_validate(ctx, _make_spec(input_schema={}))
        assert ctx.validated_params == {"x": 1}
        assert len(ctx.stage_errors) == 0

    def test_valid_params_pass(self):
        schema = {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["url"],
        }
        pipeline = ToolPipeline(strict_mode=True)
        ctx = ToolExecutionContext(tool_name="t", raw_params={"url": "http://example.com", "limit": 10})
        pipeline._stage_schema_validate(ctx, _make_spec(input_schema=schema))
        assert ctx.validated_params is not None
        assert len(ctx.stage_errors) == 0

    def test_missing_required_field_strict(self):
        schema = {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        }
        pipeline = ToolPipeline(strict_mode=True)
        ctx = ToolExecutionContext(tool_name="t", raw_params={"limit": 5})
        pipeline._stage_schema_validate(ctx, _make_spec(input_schema=schema))
        assert ctx.has_fatal_error is True
        assert "url" in ctx.stage_errors[0].message.lower()

    def test_missing_required_field_lenient(self):
        schema = {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        }
        pipeline = ToolPipeline(strict_mode=False)
        ctx = ToolExecutionContext(tool_name="t", raw_params={"limit": 5})
        pipeline._stage_schema_validate(ctx, _make_spec(input_schema=schema))
        assert ctx.has_fatal_error is False
        assert ctx.validated_params == {"limit": 5}  # 降级继续
        assert len(ctx.stage_errors) == 1

    def test_wrong_type_strict(self):
        schema = {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        }
        pipeline = ToolPipeline(strict_mode=True)
        ctx = ToolExecutionContext(tool_name="t", raw_params={"limit": "not_int"})
        pipeline._stage_schema_validate(ctx, _make_spec(input_schema=schema))
        assert ctx.has_fatal_error is True

    def test_no_spec_passes(self):
        pipeline = ToolPipeline(strict_mode=True)
        ctx = ToolExecutionContext(tool_name="t", raw_params={"a": 1})
        pipeline._stage_schema_validate(ctx, None)
        assert ctx.validated_params == {"a": 1}


# ---------------------------------------------------------------------------
# 语义校验测试
# ---------------------------------------------------------------------------

class TestSemanticValidation:
    def test_builtin_file_path_blocked(self):
        """系统路径写操作应被拦截。"""
        pipeline = ToolPipeline(strict_mode=True)
        ctx = ToolExecutionContext(
            tool_name="file.read_write",
            raw_params={"action": "write", "file_path": "/etc/passwd"},
        )
        ctx.validated_params = copy.deepcopy(ctx.raw_params)
        pipeline._stage_semantic_validate(ctx, _make_spec(name="file.read_write"))
        assert ctx.has_fatal_error is True
        assert "protected" in ctx.stage_errors[0].message.lower() or "blocked" in ctx.stage_errors[0].message.lower()

    def test_builtin_file_read_allowed(self):
        """读操作不应被拦截。"""
        pipeline = ToolPipeline(strict_mode=True)
        ctx = ToolExecutionContext(
            tool_name="file.read_write",
            raw_params={"action": "read", "file_path": "/etc/passwd"},
        )
        ctx.validated_params = copy.deepcopy(ctx.raw_params)
        pipeline._stage_semantic_validate(ctx, _make_spec(name="file.read_write"))
        assert ctx.has_fatal_error is False

    def test_normal_path_allowed(self):
        pipeline = ToolPipeline(strict_mode=True)
        ctx = ToolExecutionContext(
            tool_name="file.read_write",
            raw_params={"action": "write", "file_path": "/Users/test/output.txt"},
        )
        ctx.validated_params = copy.deepcopy(ctx.raw_params)
        pipeline._stage_semantic_validate(ctx, _make_spec(name="file.read_write"))
        assert ctx.has_fatal_error is False

    def test_custom_validate_input_reject(self):
        def my_validator(params):
            if params.get("target") == "bad":
                return "target is bad"
            return None

        pipeline = ToolPipeline(strict_mode=True)
        spec = _make_spec(validate_input=my_validator)
        ctx = ToolExecutionContext(tool_name="custom.tool", raw_params={"target": "bad"})
        ctx.validated_params = copy.deepcopy(ctx.raw_params)
        pipeline._stage_semantic_validate(ctx, spec)
        assert ctx.has_fatal_error is True
        assert "bad" in ctx.stage_errors[0].message

    def test_custom_validate_input_pass(self):
        def my_validator(params):
            return None

        pipeline = ToolPipeline(strict_mode=True)
        spec = _make_spec(validate_input=my_validator)
        ctx = ToolExecutionContext(tool_name="custom.tool", raw_params={"target": "good"})
        ctx.validated_params = copy.deepcopy(ctx.raw_params)
        pipeline._stage_semantic_validate(ctx, spec)
        assert ctx.has_fatal_error is False

    def test_semantic_lenient_mode(self):
        """降级模式下语义校验失败不 fatal。"""
        pipeline = ToolPipeline(strict_mode=False)
        ctx = ToolExecutionContext(
            tool_name="file.read_write",
            raw_params={"action": "write", "file_path": "/etc/important"},
        )
        ctx.validated_params = copy.deepcopy(ctx.raw_params)
        pipeline._stage_semantic_validate(ctx, _make_spec(name="file.read_write"))
        assert ctx.has_fatal_error is False
        assert len(ctx.stage_errors) == 1


# ---------------------------------------------------------------------------
# 上下文注入测试
# ---------------------------------------------------------------------------

class TestContextInjection:
    def test_path_expansion_tilde(self):
        pipeline = ToolPipeline()
        ctx = ToolExecutionContext(
            tool_name="file.read_write",
            raw_params={"file_path": "~/test.txt"},
        )
        ctx.validated_params = copy.deepcopy(ctx.raw_params)
        pipeline._stage_inject_context(ctx, _make_spec(), _make_state())
        assert "~" not in ctx.injected_params["file_path"]
        assert os.path.isabs(ctx.injected_params["file_path"])

    def test_relative_path_to_absolute(self):
        pipeline = ToolPipeline()
        ctx = ToolExecutionContext(
            tool_name="file.read_write",
            raw_params={"file_path": "data/output.csv"},
        )
        ctx.validated_params = copy.deepcopy(ctx.raw_params)
        pipeline._stage_inject_context(ctx, _make_spec(), _make_state())
        assert os.path.isabs(ctx.injected_params["file_path"])

    def test_required_context_injection(self):
        pipeline = ToolPipeline()
        spec = _make_spec(required_context=["session_id", "job_id", "cwd"])
        ctx = ToolExecutionContext(
            tool_name="test.tool",
            raw_params={"task": "do something"},
        )
        ctx.validated_params = copy.deepcopy(ctx.raw_params)
        state = _make_state(session_id="sess-123", job_id="job-456")
        pipeline._stage_inject_context(ctx, spec, state)
        assert ctx.injected_params["session_id"] == "sess-123"
        assert ctx.injected_params["job_id"] == "job-456"
        assert ctx.injected_params["cwd"] == str(os.getcwd())

    def test_existing_params_not_overwritten(self):
        """已有的参数不被 required_context 覆盖。"""
        pipeline = ToolPipeline()
        spec = _make_spec(required_context=["session_id"])
        ctx = ToolExecutionContext(
            tool_name="test.tool",
            raw_params={"session_id": "my-custom-id"},
        )
        ctx.validated_params = copy.deepcopy(ctx.raw_params)
        pipeline._stage_inject_context(ctx, spec, _make_state(session_id="auto-id"))
        assert ctx.injected_params["session_id"] == "my-custom-id"


# ---------------------------------------------------------------------------
# 权限检查测试
# ---------------------------------------------------------------------------

class TestPermissionCheck:
    def test_low_risk_auto_allow(self):
        pipeline = ToolPipeline()
        ctx = ToolExecutionContext(
            tool_name="web.fetch_and_extract",
            raw_params={"url": "http://example.com"},
        )
        ctx.injected_params = copy.deepcopy(ctx.raw_params)
        pipeline._stage_check_permission(ctx, _make_state())
        assert ctx.permission_result == "allow"

    def test_system_control_ask(self):
        pipeline = ToolPipeline()
        ctx = ToolExecutionContext(
            tool_name="system.control",
            raw_params={"command": "rm -rf /tmp/test"},
        )
        ctx.injected_params = copy.deepcopy(ctx.raw_params)
        pipeline._stage_check_permission(ctx, _make_state())
        assert ctx.permission_result == "ask"


# ---------------------------------------------------------------------------
# 结果规范化测试
# ---------------------------------------------------------------------------

class TestResultNormalization:
    def test_normalize_success(self):
        pipeline = ToolPipeline()
        ctx = ToolExecutionContext(tool_name="t", raw_params={})
        ctx.raw_result = {
            "status": "completed",
            "result": {"success": True, "output": "hello", "url": "http://x.com"},
        }
        pipeline._stage_normalize_result(ctx)
        r = ctx.normalized_result
        assert r.success is True
        assert r.output == "hello"
        assert r.structured_data["url"] == "http://x.com"

    def test_normalize_failure(self):
        pipeline = ToolPipeline()
        ctx = ToolExecutionContext(tool_name="t", raw_params={})
        ctx.raw_result = {
            "status": "failed",
            "result": {"success": False, "error": "timeout"},
            "error_trace": "connection timed out",
            "failure_type": "network_error",
        }
        pipeline._stage_normalize_result(ctx)
        r = ctx.normalized_result
        assert r.success is False
        assert r.error == "connection timed out"
        assert r.error_type == "network_error"

    def test_normalize_string_result(self):
        pipeline = ToolPipeline()
        ctx = ToolExecutionContext(tool_name="t", raw_params={})
        ctx.raw_result = {
            "status": "completed",
            "result": "plain text output",
        }
        pipeline._stage_normalize_result(ctx)
        r = ctx.normalized_result
        assert r.success is True
        assert r.output == "plain text output"

    def test_normalize_none_result(self):
        pipeline = ToolPipeline()
        ctx = ToolExecutionContext(tool_name="t", raw_params={})
        ctx.raw_result = None
        pipeline._stage_normalize_result(ctx)
        r = ctx.normalized_result
        assert r.success is False
        assert r.error_type == "ExecutionError"


# ---------------------------------------------------------------------------
# Pipeline 全流程集成测试
# ---------------------------------------------------------------------------

class TestPipelineFullFlow:
    @patch("core.tool_adapters.execute_tool_via_adapter")
    def test_full_success_flow(self, mock_execute):
        mock_execute.return_value = {
            "status": "completed",
            "task_type": "test_worker",
            "tool_name": "test.tool",
            "params": {"task": "hello"},
            "result": {"success": True, "output": "done"},
            "execution_trace": [],
            "failure_type": None,
            "error_trace": "",
            "risk_level": "low",
        }

        pipeline = ToolPipeline(strict_mode=False)
        spec = _make_spec(
            input_schema={
                "type": "object",
                "properties": {"task": {"type": "string"}},
            }
        )
        tool = _make_registered_tool(spec=spec)

        ctx = _run(pipeline.execute(
            "test.tool", {"task": "hello"}, _make_state(), tool
        ))

        assert ctx.raw_result is not None
        assert ctx.raw_result["status"] == "completed"
        assert ctx.normalized_result.success is True
        assert len(ctx.stage_errors) == 0
        # 所有阶段都有时间记录
        assert len(ctx.stage_timings) == 6

    @patch("core.tool_adapters.execute_tool_via_adapter")
    def test_schema_fail_strict_aborts(self, mock_execute):
        """strict 模式下 schema 校验失败，不执行后续阶段。"""
        schema = {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        }
        pipeline = ToolPipeline(strict_mode=True)
        tool = _make_registered_tool(spec=_make_spec(input_schema=schema))

        ctx = _run(pipeline.execute(
            "test.tool", {"wrong_key": 123}, _make_state(), tool
        ))

        mock_execute.assert_not_called()
        assert ctx.normalized_result.success is False
        assert "PipelineValidationError" in ctx.normalized_result.error_type

    @patch("core.tool_adapters.execute_tool_via_adapter")
    def test_schema_fail_lenient_continues(self, mock_execute):
        """降级模式下 schema 校验失败，仍继续执行。"""
        mock_execute.return_value = {
            "status": "completed",
            "result": {"success": True, "output": "ok"},
        }
        schema = {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        }
        pipeline = ToolPipeline(strict_mode=False)
        tool = _make_registered_tool(spec=_make_spec(input_schema=schema))

        ctx = _run(pipeline.execute(
            "test.tool", {"wrong_key": 123}, _make_state(), tool
        ))

        mock_execute.assert_called_once()
        assert len(ctx.stage_errors) == 1  # warning only
        assert ctx.stage_errors[0].fatal is False

    @patch("core.tool_adapters.execute_tool_via_adapter")
    def test_semantic_fail_strict_aborts(self, mock_execute):
        """strict 模式下语义校验失败，不执行后续阶段。"""
        pipeline = ToolPipeline(strict_mode=True)
        spec = _make_spec(name="file.read_write")
        tool = _make_registered_tool(spec=spec)

        ctx = _run(pipeline.execute(
            "file.read_write",
            {"action": "write", "file_path": "/etc/shadow"},
            _make_state(),
            tool,
        ))

        mock_execute.assert_not_called()
        assert ctx.normalized_result.success is False

    @patch("core.tool_adapters.execute_tool_via_adapter")
    def test_execution_exception_captured(self, mock_execute):
        mock_execute.side_effect = RuntimeError("adapter crash")

        pipeline = ToolPipeline(strict_mode=False)
        tool = _make_registered_tool()

        ctx = _run(pipeline.execute(
            "test.tool", {"task": "go"}, _make_state(), tool
        ))

        assert ctx.normalized_result.success is False
        assert "adapter crash" in (ctx.normalized_result.error or "")

    def test_stage_timings_recorded(self):
        """所有阶段的执行时间应被记录。"""
        pipeline = ToolPipeline(strict_mode=True)
        # schema 校验失败就停止，至少应有 schema_validate 时间
        schema = {"type": "object", "required": ["x"]}
        tool = _make_registered_tool(spec=_make_spec(input_schema=schema))

        ctx = _run(pipeline.execute("t", {}, _make_state(), tool))
        assert "schema_validate" in ctx.stage_timings


# ---------------------------------------------------------------------------
# _validate_file_paths 单独测试
# ---------------------------------------------------------------------------

class TestValidateFilePaths:
    def test_etc_path_blocked(self):
        assert _validate_file_paths({"action": "write", "file_path": "/etc/hosts"}) is not None

    def test_usr_path_blocked(self):
        assert _validate_file_paths({"action": "write", "file_path": "/usr/bin/python"}) is not None

    def test_home_path_allowed(self):
        assert _validate_file_paths({"action": "write", "file_path": "/home/user/data.txt"}) is None

    def test_read_always_allowed(self):
        assert _validate_file_paths({"action": "read", "file_path": "/etc/passwd"}) is None

    def test_target_path_checked(self):
        assert _validate_file_paths({"action": "write", "target_path": "/var/log/syslog"}) is not None


# ---------------------------------------------------------------------------
# get_tool_pipeline 工厂测试
# ---------------------------------------------------------------------------

class TestFactory:
    def test_default_factory(self):
        p = get_tool_pipeline()
        assert isinstance(p, ToolPipeline)

    def test_strict_override(self):
        p = get_tool_pipeline(strict_mode=True)
        assert p._strict is True
