"""
MCP (Model Context Protocol) Client --- 管理与 MCP Server 的连接和工具调用。

支持 stdio / SSE / Streamable HTTP 三种传输方式。
stdio 为主要实现，SSE 和 HTTP 为预留接口。
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from utils.logger import log_agent_action, log_error, log_warning


@dataclass
class MCPServerConfig:
    """单个 MCP Server 的配置。"""
    name: str
    transport: str                          # stdio | sse | streamable_http
    enabled: bool = True
    description: str = ""
    description_zh: str = ""
    risk_level: str = "medium"
    max_parallelism: int = 2
    # stdio 专用
    command: str = ""
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    # SSE / HTTP 专用
    url: str = ""


@dataclass
class MCPTool:
    """从 MCP Server 发现的单个工具。"""
    server_name: str
    name: str                               # MCP tool name
    description: str
    input_schema: Dict[str, Any]            # JSON Schema


class MCPClient:
    """
    管理单个 MCP Server 的生命周期。

    职责：
    1. 启动/连接 Server（stdio 子进程）
    2. 发现工具列表 (tools/list)
    3. 调用工具 (tools/call)
    4. 优雅关闭
    """

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._process: Optional[asyncio.subprocess.Process] = None
        self._tools: List[MCPTool] = []
        self._request_id: int = 0
        self._initialized: bool = False
        self._read_lock = asyncio.Lock()

    # -- 生命周期 -------------------------------------------------

    async def connect(self) -> None:
        """建立与 MCP Server 的连接。"""
        if self.config.transport == "stdio":
            await self._connect_stdio()
        elif self.config.transport == "sse":
            await self._connect_sse()
        elif self.config.transport == "streamable_http":
            await self._connect_http()
        else:
            raise ValueError(f"Unsupported MCP transport: {self.config.transport}")

        await self._initialize_handshake()
        self._initialized = True
        log_agent_action("MCP", f"Connected to server '{self.config.name}' via {self.config.transport}")

    async def disconnect(self) -> None:
        """优雅关闭连接。"""
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
            log_agent_action("MCP", f"Disconnected from server '{self.config.name}'")
        self._initialized = False

    @property
    def is_connected(self) -> bool:
        if self.config.transport == "stdio":
            return (
                self._initialized
                and self._process is not None
                and self._process.returncode is None
            )
        return self._initialized

    # -- stdio 传输 -----------------------------------------------

    async def _connect_stdio(self) -> None:
        env = os.environ.copy()
        for k, v in self.config.env.items():
            env[k] = os.path.expandvars(v)

        self._process = await asyncio.create_subprocess_exec(
            self.config.command, *self.config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

    async def _connect_sse(self) -> None:
        """SSE 传输 --- 预留接口。"""
        log_warning(f"MCP SSE transport for '{self.config.name}' is not yet implemented")

    async def _connect_http(self) -> None:
        """Streamable HTTP 传输 --- 预留接口。"""
        log_warning(f"MCP HTTP transport for '{self.config.name}' is not yet implemented")

    # -- JSON-RPC 通信 --------------------------------------------

    async def _send_jsonrpc(self, method: str, params: Optional[Dict] = None) -> Any:
        """发送 JSON-RPC 2.0 请求并等待响应。"""
        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
        }
        if params is not None:
            request["params"] = params

        if self.config.transport == "stdio":
            return await self._stdio_roundtrip(request)
        elif self.config.transport == "streamable_http":
            return await self._http_roundtrip(request)
        else:
            raise NotImplementedError(f"Transport '{self.config.transport}' send not implemented")

    async def _send_notification(self, method: str, params: Optional[Dict] = None) -> None:
        """发送 JSON-RPC 2.0 通知（无 id，不期望响应）。"""
        notification: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            notification["params"] = params

        if self.config.transport == "stdio":
            assert self._process and self._process.stdin
            payload = json.dumps(notification) + "\n"
            self._process.stdin.write(payload.encode())
            await self._process.stdin.drain()

    async def _stdio_roundtrip(self, request: Dict) -> Any:
        """通过 stdin/stdout 完成一次 JSON-RPC 请求-响应。"""
        assert self._process and self._process.stdin and self._process.stdout

        payload = json.dumps(request) + "\n"

        async with self._read_lock:
            self._process.stdin.write(payload.encode())
            await self._process.stdin.drain()

            from config.settings import settings
            timeout = getattr(settings, "MCP_TOOL_CALL_TIMEOUT", 30)

            # 读取响应行，跳过空行和非 JSON 行
            while True:
                line = await asyncio.wait_for(
                    self._process.stdout.readline(), timeout=float(timeout)
                )
                if not line:
                    raise ConnectionError(
                        f"MCP server '{self.config.name}' closed stdout unexpectedly"
                    )
                decoded = line.decode().strip()
                if not decoded:
                    continue
                try:
                    response = json.loads(decoded)
                    break
                except json.JSONDecodeError:
                    continue

        if "error" in response:
            err = response["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise RuntimeError(f"MCP server '{self.config.name}' error: {msg}")

        return response.get("result")

    async def _http_roundtrip(self, request: Dict) -> Any:
        """通过 HTTP POST 完成一次 JSON-RPC 请求-响应。"""
        try:
            import httpx
        except ImportError:
            raise ImportError("httpx is required for MCP HTTP transport: pip install httpx")

        from config.settings import settings
        timeout = getattr(settings, "MCP_TOOL_CALL_TIMEOUT", 30)

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.config.url,
                json=request,
                timeout=float(timeout),
            )
            resp.raise_for_status()
            data = resp.json()

        if "error" in data:
            err = data["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise RuntimeError(f"MCP server '{self.config.name}' error: {msg}")

        return data.get("result")

    # -- MCP 协议 -------------------------------------------------

    async def _initialize_handshake(self) -> None:
        """MCP initialize + notifications/initialized 握手。"""
        await self._send_jsonrpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "OmniCore",
                "version": "0.1.0",
            },
        })
        await self._send_notification("notifications/initialized")

    async def discover_tools(self) -> List[MCPTool]:
        """调用 tools/list 获取 Server 提供的全部工具。"""
        result = await self._send_jsonrpc("tools/list")
        self._tools = []
        for tool_def in (result or {}).get("tools", []):
            self._tools.append(MCPTool(
                server_name=self.config.name,
                name=tool_def["name"],
                description=tool_def.get("description", ""),
                input_schema=tool_def.get("inputSchema", {}),
            ))
        log_agent_action(
            "MCP",
            f"[{self.config.name}] Discovered {len(self._tools)} tools: "
            f"{[t.name for t in self._tools]}",
        )
        return self._tools

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """调用 tools/call 执行指定工具。"""
        if not self.is_connected:
            raise ConnectionError(f"MCP server '{self.config.name}' is not connected")

        result = await self._send_jsonrpc("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        return result

    @property
    def tools(self) -> List[MCPTool]:
        return list(self._tools)


# ================================================================
# MCPClientManager --- 管理所有 MCP Server 连接的单例
# ================================================================

class MCPClientManager:
    """
    管理所有 MCP Server 连接。

    职责：
    1. 从 config/mcp_servers.yaml 加载配置
    2. 连接所有 enabled 的 Server 并发现工具
    3. 汇总所有工具列表供 ToolRegistry 消费
    4. 路由工具调用到对应 Server
    """

    _instance: Optional["MCPClientManager"] = None
    _init_lock: Optional[asyncio.Lock] = None

    def __init__(self):
        self._clients: Dict[str, MCPClient] = {}
        self._tool_to_server: Dict[str, str] = {}   # full_tool_name -> server_name
        self._loaded: bool = False

    @classmethod
    async def get_instance(cls) -> "MCPClientManager":
        if cls._init_lock is None:
            cls._init_lock = asyncio.Lock()

        if cls._instance is not None and cls._instance._loaded:
            return cls._instance

        async with cls._init_lock:
            if cls._instance is not None and cls._instance._loaded:
                return cls._instance
            instance = cls()
            await instance._load_and_connect()
            cls._instance = instance
            return instance

    @classmethod
    def reset(cls) -> None:
        """重置单例（测试用）。"""
        cls._instance = None
        cls._init_lock = None

    # -- 配置加载与连接 -------------------------------------------

    async def _load_and_connect(self) -> None:
        config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "config", "mcp_servers.yaml"
        )
        config_path = os.path.normpath(config_path)

        if not os.path.exists(config_path):
            log_warning("MCP config not found at config/mcp_servers.yaml, skipping MCP initialization")
            self._loaded = True
            return

        try:
            import yaml
        except ImportError:
            log_warning("PyYAML not installed, skipping MCP initialization")
            self._loaded = True
            return

        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        from config.settings import settings
        if not getattr(settings, "MCP_ENABLED", True):
            log_agent_action("MCP", "MCP disabled via MCP_ENABLED=false")
            self._loaded = True
            return

        servers_cfg = raw.get("servers", {})
        if not servers_cfg:
            self._loaded = True
            return

        for name, server_cfg in servers_cfg.items():
            if not server_cfg.get("enabled", False):
                continue

            config = MCPServerConfig(
                name=name,
                transport=server_cfg.get("transport", "stdio"),
                enabled=True,
                description=server_cfg.get("description", ""),
                description_zh=server_cfg.get("description_zh", ""),
                risk_level=server_cfg.get("risk_level", "medium"),
                max_parallelism=server_cfg.get("max_parallelism", 2),
                command=server_cfg.get("command", ""),
                args=server_cfg.get("args", []),
                env=server_cfg.get("env", {}),
                url=server_cfg.get("url", ""),
            )

            client = MCPClient(config)
            try:
                await client.connect()
                tools = await client.discover_tools()
                self._clients[name] = client
                for tool in tools:
                    full_name = f"mcp.{name}.{tool.name}"
                    self._tool_to_server[full_name] = name
            except Exception as e:
                log_error(f"Failed to connect MCP server '{name}': {e}")

        if self._clients:
            log_agent_action(
                "MCP",
                f"Initialized {len(self._clients)} server(s), "
                f"{len(self._tool_to_server)} tool(s) available",
            )

        self._loaded = True

    # -- 工具查询 -------------------------------------------------

    def get_all_tools(self) -> List[MCPTool]:
        tools = []
        for client in self._clients.values():
            tools.extend(client.tools)
        return tools

    def get_client(self, server_name: str) -> Optional[MCPClient]:
        return self._clients.get(server_name)

    def resolve_server(self, full_tool_name: str) -> Optional[str]:
        return self._tool_to_server.get(full_tool_name)

    # -- 工具调用 -------------------------------------------------

    async def call_tool(self, full_tool_name: str, arguments: Dict[str, Any]) -> Any:
        """路由工具调用到对应 Server。full_tool_name 格式: mcp.{server}.{tool}"""
        server_name = self._tool_to_server.get(full_tool_name)
        if not server_name:
            raise ValueError(f"Unknown MCP tool: {full_tool_name}")

        client = self._clients.get(server_name)
        if client is None or not client.is_connected:
            raise ConnectionError(f"MCP server '{server_name}' is not connected")

        # 去掉 mcp.{server_name}. 前缀得到原始 tool name
        parts = full_tool_name.split(".", 2)
        if len(parts) < 3:
            raise ValueError(f"Invalid MCP tool name format: {full_tool_name}")
        original_name = parts[2]

        return await client.call_tool(original_name, arguments)

    # -- 关闭 -----------------------------------------------------

    async def shutdown(self) -> None:
        for name, client in self._clients.items():
            try:
                await client.disconnect()
            except Exception as e:
                log_warning(f"Error disconnecting MCP server '{name}': {e}")
        self._clients.clear()
        self._tool_to_server.clear()
        self._loaded = False
