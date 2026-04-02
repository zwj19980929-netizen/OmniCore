"""
P0-1: MCP (Model Context Protocol) 工具生态接入 — 单元测试。

覆盖：
- MCPServerConfig / MCPTool 数据结构
- MCPClient JSON-RPC 通信（mock subprocess）
- MCPClientManager 配置加载与工具发现
- MCPToolAdapter 适配器集成
- _register_mcp_tools 动态注册
- 策略引擎 MCP 规则
- _extract_mcp_text 文本提取
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.mcp_client import MCPClient, MCPClientManager, MCPServerConfig, MCPTool


# ── Fixtures ─────────────────────────────────────────────────────


def _make_stdio_config(name: str = "test_server", **overrides) -> MCPServerConfig:
    defaults = dict(
        name=name,
        transport="stdio",
        enabled=True,
        description="Test MCP server",
        description_zh="测试 MCP 服务器",
        risk_level="medium",
        max_parallelism=2,
        command="echo",
        args=["hello"],
    )
    defaults.update(overrides)
    return MCPServerConfig(**defaults)


def _make_http_config(name: str = "test_http", url: str = "http://localhost:9999/mcp") -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        transport="streamable_http",
        enabled=True,
        description="Test HTTP MCP server",
        risk_level="high",
        url=url,
    )


class FakeProcess:
    """模拟 asyncio.subprocess.Process 的 stdin/stdout/stderr。"""

    def __init__(self, responses: List[Dict[str, Any]]):
        self._responses = list(responses)
        self._response_idx = 0
        self.returncode = None
        self.stdin = self
        self.stdout = self
        self.stderr = AsyncMock()

    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    async def readline(self) -> bytes:
        if self._response_idx < len(self._responses):
            resp = self._responses[self._response_idx]
            self._response_idx += 1
            return (json.dumps(resp) + "\n").encode()
        return b""

    def terminate(self) -> None:
        self.returncode = -15

    async def wait(self) -> int:
        self.returncode = 0
        return 0


# ── MCPServerConfig Tests ────────────────────────────────────────


class TestMCPServerConfig:
    def test_stdio_config_defaults(self):
        cfg = _make_stdio_config()
        assert cfg.transport == "stdio"
        assert cfg.enabled is True
        assert cfg.max_parallelism == 2
        assert cfg.url == ""

    def test_http_config(self):
        cfg = _make_http_config()
        assert cfg.transport == "streamable_http"
        assert cfg.url == "http://localhost:9999/mcp"
        assert cfg.command == ""


# ── MCPClient Tests ──────────────────────────────────────────────


class TestMCPClient:
    def test_connect_and_discover_tools(self):
        """stdio 连接 + initialize 握手 + tools/list 工具发现。"""
        async def _impl():
            init_response = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
            tools_response = {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "tools": [
                        {
                            "name": "read_file",
                            "description": "Read file contents",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"path": {"type": "string"}},
                            },
                        },
                        {
                            "name": "write_file",
                            "description": "Write to a file",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "content": {"type": "string"},
                                },
                            },
                        },
                    ]
                },
            }

            fake_proc = FakeProcess([init_response, tools_response])
            config = _make_stdio_config()
            client = MCPClient(config)

            with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
                await client.connect()
                assert client.is_connected

                tools = await client.discover_tools()
                assert len(tools) == 2
                assert tools[0].name == "read_file"
                assert tools[0].server_name == "test_server"
                assert tools[1].name == "write_file"
                assert "path" in tools[0].input_schema.get("properties", {})

        asyncio.run(_impl())

    def test_call_tool_returns_result(self):
        """tools/call 成功执行。"""
        async def _impl():
            init_response = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
            call_response = {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "content": [{"type": "text", "text": "Hello, World!"}]
                },
            }

            fake_proc = FakeProcess([init_response, call_response])
            config = _make_stdio_config()
            client = MCPClient(config)

            with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
                await client.connect()
                result = await client.call_tool("read_file", {"path": "/tmp/test.txt"})

                assert result is not None
                assert result["content"][0]["text"] == "Hello, World!"

        asyncio.run(_impl())

    def test_jsonrpc_error_raises(self):
        """Server 返回 JSON-RPC error 时抛异常。"""
        async def _impl():
            init_response = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
            error_response = {
                "jsonrpc": "2.0",
                "id": 2,
                "error": {"code": -32602, "message": "Invalid params"},
            }

            fake_proc = FakeProcess([init_response, error_response])
            config = _make_stdio_config()
            client = MCPClient(config)

            with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
                await client.connect()
                with pytest.raises(RuntimeError, match="Invalid params"):
                    await client.call_tool("bad_tool", {})

        asyncio.run(_impl())

    def test_disconnect_terminates_process(self):
        async def _impl():
            init_response = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
            fake_proc = FakeProcess([init_response])
            config = _make_stdio_config()
            client = MCPClient(config)

            with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
                await client.connect()
                assert client.is_connected

                await client.disconnect()
                assert not client.is_connected

        asyncio.run(_impl())

    def test_call_tool_when_disconnected_raises(self):
        async def _impl():
            config = _make_stdio_config()
            client = MCPClient(config)
            with pytest.raises(ConnectionError, match="not connected"):
                await client.call_tool("read_file", {})

        asyncio.run(_impl())

    def test_discover_tools_empty_result(self):
        """Server 返回空工具列表。"""
        async def _impl():
            init_response = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
            tools_response = {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}

            fake_proc = FakeProcess([init_response, tools_response])
            config = _make_stdio_config()
            client = MCPClient(config)

            with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
                await client.connect()
                tools = await client.discover_tools()
                assert tools == []

        asyncio.run(_impl())


# ── MCPClientManager Tests ───────────────────────────────────────


class TestMCPClientManager:
    def setup_method(self):
        MCPClientManager.reset()

    def test_no_config_file_graceful_skip(self, tmp_path, monkeypatch):
        """缺少 mcp_servers.yaml 时优雅跳过。"""
        async def _impl():
            monkeypatch.setattr(
                "core.mcp_client.os.path.exists",
                lambda p: False,
            )
            MCPClientManager.reset()
            manager = await MCPClientManager.get_instance()
            assert manager.get_all_tools() == []

        asyncio.run(_impl())

    def test_mcp_disabled_skips_init(self, monkeypatch):
        """MCP_ENABLED=false 时跳过初始化。"""
        async def _impl():
            from config.settings import settings
            monkeypatch.setattr(settings, "MCP_ENABLED", False)
            MCPClientManager.reset()
            manager = await MCPClientManager.get_instance()
            assert manager.get_all_tools() == []

        asyncio.run(_impl())

    def test_call_tool_routes_to_correct_server(self):
        """call_tool 路由到正确的 Server。"""
        async def _impl():
            MCPClientManager.reset()
            manager = MCPClientManager()

            mock_client = MagicMock(spec=MCPClient)
            mock_client.is_connected = True
            mock_client.tools = [
                MCPTool(server_name="fs", name="read_file", description="Read", input_schema={}),
            ]
            mock_client.config = _make_stdio_config(name="fs")
            mock_client.call_tool = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})

            manager._clients["fs"] = mock_client
            manager._tool_to_server["mcp.fs.read_file"] = "fs"
            manager._loaded = True

            result = await manager.call_tool("mcp.fs.read_file", {"path": "/tmp/x"})
            mock_client.call_tool.assert_awaited_once_with("read_file", {"path": "/tmp/x"})
            assert result["content"][0]["text"] == "ok"

        asyncio.run(_impl())

    def test_call_unknown_tool_raises(self):
        async def _impl():
            MCPClientManager.reset()
            manager = MCPClientManager()
            manager._loaded = True
            with pytest.raises(ValueError, match="Unknown MCP tool"):
                await manager.call_tool("mcp.nonexistent.tool", {})

        asyncio.run(_impl())

    def test_get_all_tools_aggregates(self):
        async def _impl():
            MCPClientManager.reset()
            manager = MCPClientManager()
            manager._loaded = True

            mock1 = MagicMock(spec=MCPClient)
            mock1.tools = [MCPTool("s1", "t1", "desc1", {})]
            mock2 = MagicMock(spec=MCPClient)
            mock2.tools = [MCPTool("s2", "t2", "desc2", {}), MCPTool("s2", "t3", "desc3", {})]

            manager._clients = {"s1": mock1, "s2": mock2}

            all_tools = manager.get_all_tools()
            assert len(all_tools) == 3
            names = {t.name for t in all_tools}
            assert names == {"t1", "t2", "t3"}

        asyncio.run(_impl())


# ── MCPToolAdapter Tests ────────────────────────────────────────


class TestMCPToolAdapter:
    def test_adapter_success(self):
        async def _impl():
            from core.tool_adapters import MCPToolAdapter
            from core.tool_protocol import ToolSpec
            from core.tool_registry import RegisteredTool

            adapter = MCPToolAdapter()
            registered = RegisteredTool(
                spec=ToolSpec(
                    name="mcp.fs.read_file",
                    task_type="mcp_handler",
                    description="Read file",
                    risk_level="medium",
                    tags=["mcp", "fs"],
                    input_schema={},
                ),
                adapter_name="mcp_adapter",
            )
            task = {
                "task_id": "t1",
                "tool_name": "mcp.fs.read_file",
                "task_type": "mcp_handler",
                "params": {"path": "/tmp/test.txt"},
                "description": "Read a file",
                "execution_trace": [],
                "risk_level": "medium",
            }

            mock_result = {"content": [{"type": "text", "text": "file contents here"}]}
            mock_manager = AsyncMock()
            mock_manager.call_tool = AsyncMock(return_value=mock_result)

            with patch("core.mcp_client.MCPClientManager.get_instance", return_value=mock_manager):
                outcome = await adapter.execute(task, {}, registered)

            assert outcome["status"] == "completed"
            assert outcome["result"]["success"] is True
            assert outcome["result"]["output"] == "file contents here"
            assert "shared_memory" not in outcome  # R2: shared_memory removed from outcomes

        asyncio.run(_impl())

    def test_adapter_connection_error(self):
        async def _impl():
            from core.tool_adapters import MCPToolAdapter
            from core.tool_protocol import ToolSpec
            from core.tool_registry import RegisteredTool

            adapter = MCPToolAdapter()
            registered = RegisteredTool(
                spec=ToolSpec(
                    name="mcp.fs.read_file",
                    task_type="mcp_handler",
                    description="Read file",
                ),
                adapter_name="mcp_adapter",
            )
            task = {
                "task_id": "t1",
                "tool_name": "mcp.fs.read_file",
                "task_type": "mcp_handler",
                "params": {"path": "/tmp/test.txt"},
                "description": "Read a file",
                "execution_trace": [],
                "risk_level": "medium",
            }

            mock_manager = AsyncMock()
            mock_manager.call_tool = AsyncMock(side_effect=ConnectionError("server down"))

            with patch("core.mcp_client.MCPClientManager.get_instance", return_value=mock_manager):
                outcome = await adapter.execute(task, {}, registered)

            assert outcome["status"] == "failed"
            assert outcome["result"]["success"] is False
            assert "server down" in outcome["error_trace"]

        asyncio.run(_impl())

    def test_adapter_generic_error(self):
        async def _impl():
            from core.tool_adapters import MCPToolAdapter
            from core.tool_protocol import ToolSpec
            from core.tool_registry import RegisteredTool

            adapter = MCPToolAdapter()
            registered = RegisteredTool(
                spec=ToolSpec(
                    name="mcp.db.query",
                    task_type="mcp_handler",
                    description="Query database",
                ),
                adapter_name="mcp_adapter",
            )
            task = {
                "task_id": "t2",
                "tool_name": "mcp.db.query",
                "task_type": "mcp_handler",
                "params": {"sql": "SELECT 1"},
                "description": "Test query",
                "execution_trace": [],
                "risk_level": "high",
            }

            mock_manager = AsyncMock()
            mock_manager.call_tool = AsyncMock(side_effect=RuntimeError("timeout"))

            with patch("core.mcp_client.MCPClientManager.get_instance", return_value=mock_manager):
                outcome = await adapter.execute(task, {}, registered)

            assert outcome["status"] == "failed"
            assert "timeout" in outcome["error_trace"]

        asyncio.run(_impl())


# ── _extract_mcp_text Tests ─────────────────────────────────────


class TestExtractMCPText:
    def test_standard_content_array(self):
        from core.tool_adapters import _extract_mcp_text

        result = {"content": [{"type": "text", "text": "Hello"}, {"type": "text", "text": "World"}]}
        assert _extract_mcp_text(result) == "Hello\nWorld"

    def test_mixed_content_types(self):
        from core.tool_adapters import _extract_mcp_text

        result = {
            "content": [
                {"type": "text", "text": "Text part"},
                {"type": "image", "data": "base64..."},
                {"type": "text", "text": "More text"},
            ]
        }
        assert _extract_mcp_text(result) == "Text part\nMore text"

    def test_none_result(self):
        from core.tool_adapters import _extract_mcp_text

        assert _extract_mcp_text(None) == ""

    def test_string_result(self):
        from core.tool_adapters import _extract_mcp_text

        assert _extract_mcp_text("plain string") == "plain string"

    def test_empty_content_array(self):
        from core.tool_adapters import _extract_mcp_text

        result = {"content": []}
        assert _extract_mcp_text(result) == str(result)

    def test_dict_without_content(self):
        from core.tool_adapters import _extract_mcp_text

        result = {"data": "something"}
        assert _extract_mcp_text(result) == str(result)


# ── Policy Engine MCP Rules Tests ───────────────────────────────


class TestMCPPolicyRules:
    def test_mcp_read_tool_auto_approve(self):
        from core.policy_engine import evaluate_task_policy

        decision = evaluate_task_policy({
            "task_type": "mcp_handler",
            "tool_name": "mcp.fs.read_file",
            "description": "Read contents of a config file",
            "params": {"path": "/tmp/config.yaml"},
        })
        assert decision.requires_confirmation is False

    def test_mcp_write_tool_requires_confirmation(self):
        from core.policy_engine import evaluate_task_policy

        decision = evaluate_task_policy({
            "task_type": "mcp_handler",
            "tool_name": "mcp.fs.write_file",
            "description": "Write data to a file",
            "params": {"path": "/tmp/output.txt", "content": "data"},
        })
        assert decision.requires_confirmation is True
        assert decision.risk_level == "high"

    def test_mcp_delete_tool_requires_confirmation(self):
        from core.policy_engine import evaluate_task_policy

        decision = evaluate_task_policy({
            "task_type": "mcp_handler",
            "tool_name": "mcp.github.delete_branch",
            "description": "Delete a feature branch",
            "params": {},
        })
        assert decision.requires_confirmation is True

    def test_mcp_send_message_requires_confirmation(self):
        from core.policy_engine import evaluate_task_policy

        decision = evaluate_task_policy({
            "task_type": "mcp_handler",
            "tool_name": "mcp.slack.send_message",
            "description": "Send a Slack message",
            "params": {"channel": "#general", "text": "hello"},
        })
        assert decision.requires_confirmation is True

    def test_mcp_list_tool_auto_approve(self):
        from core.policy_engine import evaluate_task_policy

        decision = evaluate_task_policy({
            "task_type": "mcp_handler",
            "tool_name": "mcp.github.list_issues",
            "description": "List open issues",
            "params": {"repo": "owner/repo"},
        })
        assert decision.requires_confirmation is False

    def test_mcp_create_issue_requires_confirmation(self):
        from core.policy_engine import evaluate_task_policy

        decision = evaluate_task_policy({
            "task_type": "mcp_handler",
            "tool_name": "mcp.github.create_issue",
            "description": "Create a new issue",
            "params": {},
        })
        assert decision.requires_confirmation is True

    def test_mcp_description_based_high_risk(self):
        """即使 tool_name 不含高危词，description 中有也应拦截。"""
        from core.policy_engine import evaluate_task_policy

        decision = evaluate_task_policy({
            "task_type": "mcp_handler",
            "tool_name": "mcp.custom.execute",
            "description": "Delete all expired records from the database",
            "params": {},
        })
        assert decision.requires_confirmation is True


# ── Dynamic Registration Tests ───────────────────────────────────


class TestMCPDynamicRegistration:
    def test_mcp_adapter_is_in_adapter_registry(self):
        from core.tool_adapters import build_tool_adapter_registry

        registry = build_tool_adapter_registry()
        adapter = registry.get("mcp_adapter")
        assert adapter is not None
        assert adapter.__class__.__name__ == "MCPToolAdapter"

    def test_mcp_handler_in_agents_yaml(self):
        """agents.yaml 中包含 mcp_handler 条目。"""
        import yaml

        config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "config", "agents.yaml"
        )
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        agents = data.get("agents", {})
        assert "mcp_handler" in agents
        assert agents["mcp_handler"]["adapter"] == "mcp_adapter"

    def test_mcp_settings_defaults(self):
        """MCP 配置项有正确的默认值。"""
        from config.settings import Settings

        s = Settings()
        assert s.MCP_ENABLED is True
        assert s.MCP_TOOL_CALL_TIMEOUT == 30
