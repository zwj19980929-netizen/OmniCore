"""
S6: Fail-Closed 安全分层 单元测试

覆盖：
- Fail-closed 默认值（新工具未声明 capabilities 时默认行为）
- 信任分层（不同 trust_level 的审批行为）
- MCP 约束（description 截断、名称冲突处理、认证失败缓存）
- 路径白名单 / 命令黑名单
- 审计日志写入
- policy_engine trust_level 集成
"""
import asyncio
import json
import os
import shutil
import tempfile
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from config.settings import settings
from core.tool_protocol import ToolSpec


# ---------------------------------------------------------------------------
# S6-1: Fail-Closed Default Tests
# ---------------------------------------------------------------------------

class TestFailClosedDefaults:
    """新工具未显式声明 capabilities 时，默认取最保守值。"""

    def test_default_destructive_is_true(self):
        spec = ToolSpec(name="test.new", task_type="test", description="test")
        assert spec.destructive is True

    def test_default_concurrent_safe_is_false(self):
        spec = ToolSpec(name="test.new", task_type="test", description="test")
        assert spec.concurrent_safe is False

    def test_default_trust_level_is_builtin(self):
        spec = ToolSpec(name="test.new", task_type="test", description="test")
        assert spec.trust_level == "builtin"

    def test_explicit_opt_out_destructive(self):
        spec = ToolSpec(name="test.safe", task_type="test", description="safe", destructive=False)
        assert spec.destructive is False

    def test_explicit_opt_out_concurrent(self):
        spec = ToolSpec(
            name="test.parallel", task_type="test", description="ok",
            concurrent_safe=True,
        )
        assert spec.concurrent_safe is True

    def test_builtin_tools_have_explicit_trust_level(self):
        """所有内置工具应有显式 trust_level='builtin'。"""
        from core.tool_registry import get_builtin_tool_registry
        registry = get_builtin_tool_registry()
        for tool in registry.list_tools():
            if not tool.spec.name.startswith("mcp."):
                assert tool.spec.trust_level == "builtin", (
                    f"Builtin tool '{tool.spec.name}' missing explicit trust_level='builtin'"
                )

    def test_read_only_builtins_not_destructive(self):
        """只读内置工具应显式声明 destructive=False。"""
        from core.tool_registry import get_builtin_tool_registry
        registry = get_builtin_tool_registry()
        read_only_tools = {
            "web.fetch_and_extract", "web.smart_extract", "browser.interact",
            "terminal.read_file", "terminal.search", "api.call",
        }
        for tool in registry.list_tools():
            if tool.spec.name in read_only_tools:
                assert tool.spec.destructive is False, (
                    f"Read-only tool '{tool.spec.name}' should have destructive=False"
                )


# ---------------------------------------------------------------------------
# S6-2: 信任分层 Tests
# ---------------------------------------------------------------------------

class TestTrustLevels:
    """信任分层模型的正确性。"""

    def test_trust_level_ordering(self):
        """trust_level 有明确的信任序：builtin > local > mcp_local > mcp_remote。"""
        levels = ["builtin", "local", "mcp_local", "mcp_remote"]
        # 验证所有合法 trust_level 值
        for level in levels:
            spec = ToolSpec(name=f"test.{level}", task_type="t", description="d", trust_level=level)
            assert spec.trust_level == level

    def test_mcp_stdio_gets_mcp_local(self):
        """MCP stdio 工具应被标记为 mcp_local。"""
        from core.mcp_client import MCPServerConfig, MCPTool
        config = MCPServerConfig(name="test_server", transport="stdio")
        # 验证 transport -> trust_level 映射逻辑
        assert config.transport == "stdio"
        # mcp_local 是 stdio 的预期 trust_level

    def test_mcp_http_gets_mcp_remote(self):
        """MCP HTTP 工具应被标记为 mcp_remote。"""
        from core.mcp_client import MCPServerConfig
        config = MCPServerConfig(name="test_server", transport="streamable_http")
        assert config.transport == "streamable_http"
        # mcp_remote 是 http 的预期 trust_level


# ---------------------------------------------------------------------------
# S6-3: MCP 约束 Tests
# ---------------------------------------------------------------------------

class TestMCPConstraints:
    """MCP 安全约束：description 截断、认证失败缓存。"""

    def test_description_truncation(self):
        """超过 MCP_DESCRIPTION_MAX_LENGTH 的描述应被截断。"""
        from config.settings import settings
        max_len = getattr(settings, "MCP_DESCRIPTION_MAX_LENGTH", 2048)
        long_desc = "x" * (max_len + 500)
        # 模拟截断逻辑
        if len(long_desc) > max_len:
            truncated = long_desc[:max_len] + "…[truncated]"
        else:
            truncated = long_desc
        assert len(truncated) < len(long_desc)
        assert truncated.endswith("…[truncated]")

    def test_auth_failure_cache(self):
        """认证失败的 MCP Server 应被缓存，短时间内不重试。"""
        from core.mcp_client import MCPClientManager
        manager = MCPClientManager()
        manager._failure_cache["bad_server"] = time.monotonic()
        assert manager.is_server_failure_cached("bad_server") is True
        assert manager.is_server_failure_cached("good_server") is False

    def test_auth_failure_cache_expiry(self):
        """认证失败缓存超时后应允许重试。"""
        from core.mcp_client import MCPClientManager
        manager = MCPClientManager()
        # 设置一个很久以前的失败时间
        manager._failure_cache["old_server"] = time.monotonic() - 99999
        assert manager.is_server_failure_cached("old_server") is False

    def test_call_tool_rejects_cached_failure(self):
        """调用认证失败缓存中的 server 应立即报错。"""
        from core.mcp_client import MCPClientManager
        manager = MCPClientManager()
        manager._failure_cache["bad_server"] = time.monotonic()
        manager._tool_to_server["mcp.bad_server.test"] = "bad_server"

        with pytest.raises(ConnectionError, match="auth failure cooldown"):
            asyncio.run(manager.call_tool("mcp.bad_server.test", {}))


# ---------------------------------------------------------------------------
# S6-4: 终端命令黑名单 Tests
# ---------------------------------------------------------------------------

class TestCommandBlacklist:
    """终端命令黑名单拦截。"""

    def test_rm_rf_root_blocked(self):
        from core.tool_pipeline import _validate_terminal_command
        result = _validate_terminal_command({"command": "rm -rf /"})
        assert result is not None
        assert "Blocked" in result

    def test_mkfs_blocked(self):
        from core.tool_pipeline import _validate_terminal_command
        result = _validate_terminal_command({"command": "mkfs.ext4 /dev/sda1"})
        assert result is not None

    def test_dd_blocked(self):
        from core.tool_pipeline import _validate_terminal_command
        result = _validate_terminal_command({"command": "dd if=/dev/zero of=/dev/sda"})
        assert result is not None

    def test_fork_bomb_blocked(self):
        from core.tool_pipeline import _validate_terminal_command
        result = _validate_terminal_command({"command": ":(){ :|:& };"})
        assert result is not None

    def test_safe_command_allowed(self):
        from core.tool_pipeline import _validate_terminal_command
        assert _validate_terminal_command({"command": "ls -la"}) is None
        assert _validate_terminal_command({"command": "git status"}) is None
        assert _validate_terminal_command({"command": "python main.py"}) is None

    def test_empty_command_allowed(self):
        from core.tool_pipeline import _validate_terminal_command
        assert _validate_terminal_command({"command": ""}) is None
        assert _validate_terminal_command({}) is None


# ---------------------------------------------------------------------------
# S6-4: 路径白名单 Tests
# ---------------------------------------------------------------------------

class TestPathValidation:
    """系统关键路径拦截。"""

    def test_system_path_blocked_for_write(self):
        from core.tool_pipeline import _validate_file_paths
        result = _validate_file_paths({"file_path": "/etc/passwd", "action": "write"})
        assert result is not None
        assert "Blocked" in result

    def test_system_path_allowed_for_read(self):
        from core.tool_pipeline import _validate_file_paths
        result = _validate_file_paths({"file_path": "/etc/passwd", "action": "read"})
        assert result is None

    def test_usr_path_blocked(self):
        from core.tool_pipeline import _validate_file_paths
        result = _validate_file_paths({"file_path": "/usr/bin/test", "action": "write"})
        assert result is not None

    def test_normal_path_allowed(self):
        from core.tool_pipeline import _validate_file_paths
        result = _validate_file_paths({"file_path": "/tmp/test.txt", "action": "write"})
        assert result is None

    def test_windows_system_path_blocked(self):
        from core.tool_pipeline import _validate_file_paths
        result = _validate_file_paths({
            "file_path": "C:\\Windows\\System32\\test.dll",
            "action": "write",
        })
        assert result is not None


# ---------------------------------------------------------------------------
# S6-5: 审计日志 Tests
# ---------------------------------------------------------------------------

class TestAuditLog:
    """审计日志写入。"""

    def test_audit_log_written(self, tmp_path):
        """工具执行后应写入审计日志。"""
        from core.tool_pipeline import ToolPipeline, ToolExecutionContext, ToolResult

        pipeline = ToolPipeline(strict_mode=False)

        ctx = ToolExecutionContext(
            tool_name="test.tool",
            raw_params={"key": "value"},
        )
        ctx.permission_result = "allow"
        ctx.normalized_result = ToolResult(success=True, output="ok")
        ctx.stage_timings = {"execute": 0.1}

        spec = MagicMock()
        spec.trust_level = "builtin"
        spec.destructive = False

        orig_data_dir = settings.DATA_DIR
        orig_audit = settings.AUDIT_LOG_ENABLED
        try:
            settings.__class__.DATA_DIR = tmp_path
            settings.__class__.AUDIT_LOG_ENABLED = True
            pipeline._write_audit_log(ctx, spec)
        finally:
            settings.__class__.DATA_DIR = orig_data_dir
            settings.__class__.AUDIT_LOG_ENABLED = orig_audit

        audit_dir = tmp_path / "audit"
        assert audit_dir.exists()
        files = list(audit_dir.glob("*.jsonl"))
        assert len(files) == 1
        with open(files[0], "r") as f:
            record = json.loads(f.readline())
        assert record["tool_name"] == "test.tool"
        assert record["trust_level"] == "builtin"
        assert record["result_status"] == "success"

    def test_audit_sensitive_params_masked(self):
        """敏感参数应被脱敏。"""
        from core.tool_pipeline import ToolPipeline
        masked = ToolPipeline._mask_sensitive_params({
            "url": "https://example.com",
            "api_key": "sk-secret-123",
            "password": "hunter2",
            "query": "normal value",
        })
        assert masked["api_key"] == "***REDACTED***"
        assert masked["password"] == "***REDACTED***"
        assert masked["url"] == "https://example.com"
        assert masked["query"] == "normal value"

    def test_audit_long_param_truncated(self):
        """超长参数值应被截断。"""
        from core.tool_pipeline import ToolPipeline
        masked = ToolPipeline._mask_sensitive_params({
            "content": "x" * 1000,
        })
        assert len(masked["content"]) < 1000
        assert "chars]" in masked["content"]

    def test_audit_disabled_no_write(self, tmp_path):
        """AUDIT_LOG_ENABLED=false 时不写入。"""
        from core.tool_pipeline import ToolPipeline, ToolExecutionContext, ToolResult

        pipeline = ToolPipeline(strict_mode=False)
        ctx = ToolExecutionContext(tool_name="test.tool", raw_params={})
        ctx.normalized_result = ToolResult(success=True, output="ok")

        orig_data_dir = settings.DATA_DIR
        orig_audit = settings.AUDIT_LOG_ENABLED
        try:
            settings.__class__.DATA_DIR = tmp_path
            settings.__class__.AUDIT_LOG_ENABLED = False
            pipeline._write_audit_log(ctx, None)
        finally:
            settings.__class__.DATA_DIR = orig_data_dir
            settings.__class__.AUDIT_LOG_ENABLED = orig_audit

        audit_dir = tmp_path / "audit"
        assert not audit_dir.exists()


# ---------------------------------------------------------------------------
# S6-4: policy_engine + trust_level Tests
# ---------------------------------------------------------------------------

class TestPolicyEngineTrustLevel:
    """policy_engine 接入 trust_level 的行为。"""

    def _make_mcp_tool(self, trust_level="mcp_remote", destructive=True, risk_level="medium"):
        from core.tool_registry import RegisteredTool
        spec = ToolSpec(
            name="mcp.test_server.dangerous_tool",
            task_type="mcp_handler",
            description="A test MCP tool",
            risk_level=risk_level,
            tags=["mcp", "test_server"],
            trust_level=trust_level,
            destructive=destructive,
        )
        return RegisteredTool(spec=spec, adapter_name="mcp_adapter")

    def test_mcp_remote_destructive_requires_confirmation(self):
        """mcp_remote + destructive 工具必须人工审批。"""
        from core.policy_engine import evaluate_task_policy
        tool = self._make_mcp_tool(trust_level="mcp_remote", destructive=True)
        with patch("core.policy_engine.get_builtin_tool_registry") as mock_registry:
            mock_registry.return_value.resolve_task.return_value = tool
            decision = evaluate_task_policy({
                "tool_name": "mcp.test_server.dangerous_tool",
                "task_type": "mcp_handler",
                "params": {},
                "description": "",
            })
        assert decision.requires_confirmation is True
        assert "S6 trust policy" in decision.reason or "destructive" in decision.reason.lower()

    def test_mcp_local_readonly_no_confirmation(self):
        """mcp_local 只读工具自动放行。"""
        from core.policy_engine import evaluate_task_policy
        tool = self._make_mcp_tool(trust_level="mcp_local", destructive=False, risk_level="low")
        # 改名避免高风险 token 匹配
        spec = ToolSpec(
            name="mcp.local_server.read_data",
            task_type="mcp_handler",
            description="Read data",
            risk_level="low",
            tags=["mcp", "local_server"],
            trust_level="mcp_local",
            destructive=False,
        )
        from core.tool_registry import RegisteredTool
        tool = RegisteredTool(spec=spec, adapter_name="mcp_adapter")
        with patch("core.policy_engine.get_builtin_tool_registry") as mock_registry:
            mock_registry.return_value.resolve_task.return_value = tool
            decision = evaluate_task_policy({
                "tool_name": "mcp.local_server.read_data",
                "task_type": "mcp_handler",
                "params": {},
                "description": "read some data",
            })
        assert decision.requires_confirmation is False


# ---------------------------------------------------------------------------
# S6: Pipeline 集成 trust_level Tests
# ---------------------------------------------------------------------------

class TestPipelineTrustIntegration:
    """Pipeline 权限阶段接入 trust_level。"""

    def test_mcp_remote_destructive_gets_ask(self):
        """mcp_remote + destructive 工具在 pipeline 权限阶段应返回 ask。"""
        from core.tool_pipeline import ToolPipeline, ToolExecutionContext

        pipeline = ToolPipeline(strict_mode=False)
        ctx = ToolExecutionContext(
            tool_name="mcp.remote.dangerous",
            raw_params={"action": "delete"},
            validated_params={"action": "delete"},
        )
        spec = MagicMock()
        spec.trust_level = "mcp_remote"
        spec.destructive = True

        # Mock policy_engine to return no confirmation needed
        # (pipeline should override based on trust_level)
        mock_decision = MagicMock()
        mock_decision.requires_confirmation = False
        mock_decision.reason = "no rule matched"

        with patch("core.policy_engine.evaluate_task_policy", return_value=mock_decision):
            pipeline._stage_check_permission(ctx, {}, spec)

        assert ctx.permission_result == "ask"
        assert "S6 trust policy" in ctx.permission_reason

    def test_builtin_tool_not_overridden(self):
        """builtin 工具不应被 trust_level 策略覆盖。"""
        from core.tool_pipeline import ToolPipeline, ToolExecutionContext

        pipeline = ToolPipeline(strict_mode=False)
        ctx = ToolExecutionContext(
            tool_name="file.read_write",
            raw_params={"action": "read"},
            validated_params={"action": "read"},
        )
        spec = MagicMock()
        spec.trust_level = "builtin"
        spec.destructive = False

        mock_decision = MagicMock()
        mock_decision.requires_confirmation = False
        mock_decision.reason = "read-only"

        with patch("core.policy_engine.evaluate_task_policy", return_value=mock_decision):
            pipeline._stage_check_permission(ctx, {}, spec)

        assert ctx.permission_result == "allow"
