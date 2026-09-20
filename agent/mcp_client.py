# @File:     mcp_client.py
# @Author:   mjh
# @DateTime: 2026/09/20
"""MCP 客户端：连接外部 MCP server（stdio 子进程 / Streamable HTTP），把远端工具
以 mcp__<server>__<tool> 命名注入注册表。官方 SDK 是 asyncio，而 AgentLoop 全同步——
MCPManager 持单个后台事件循环线程承载全部 server 会话，工具调用经
run_coroutine_threadsafe 桥接（超时转 ToolError 回灌自愈）。"""
import asyncio
import concurrent.futures
import threading
from typing import TYPE_CHECKING

import httpx2
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .tools.base import Tool, ToolError, truncate
from .tools.registry import register_tool, unregister_prefix

if TYPE_CHECKING:
    from .config import MCPServerConfig

MCP_TOOL_PREFIX = "mcp__"


def flatten_content(blocks) -> str:
    """CallToolResult.content 块列表拍平为文本：TextContent 取 text，
    其余（Image/Audio/Embedded）给占位说明——二进制不回灌模型。"""
    parts = []
    for b in blocks or []:
        text = getattr(b, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
        else:
            parts.append(f"[{type(b).__name__}：非文本内容，不回灌]")
    return "\n".join(parts) if parts else "(无内容)"


class MCPTool(Tool):
    """远端工具适配器：name 加 mcp__ 前缀防撞内置；参数原样透传（注册时
    validate=False——远端 schema 千奇百怪，本地简化校验会错杀，错误由远端回灌）。"""

    def __init__(self, server: str, name: str, description: str | None,
                 schema: dict | None, call):
        self.name = f"{MCP_TOOL_PREFIX}{server}__{name}"
        self.description = description or f"MCP server {server} 提供的工具"
        self.parameters = (schema if isinstance(schema, dict) and schema
                           else {"type": "object", "properties": {}})
        self._call = call

    def execute(self, **kwargs) -> str:
        return self._call(kwargs)


def make_call_bridge(conn: "MCPConnection", server: str, remote_name: str, timeout: int):
    """闭包：同步调用 → 拍平 → isError/超时/异常统一转 ToolError（错误回灌自愈）。"""
    def call(kwargs: dict) -> str:
        try:
            result = conn.call_tool(remote_name, kwargs, timeout)
        except concurrent.futures.TimeoutError as e:
            raise ToolError(f"MCP server {server} 工具 {remote_name} 响应超时（{timeout}s），"
                            f"请重试或改用其他方式") from e
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"MCP server {server} 调用失败: {type(e).__name__}: {e}") from e
        text = flatten_content(getattr(result, "content", None))
        if getattr(result, "is_error", getattr(result, "isError", False)):
            raise ToolError(text)
        return truncate(text)
    return call


class MCPConnection:
    """单个 server 的会话：生命周期协程常驻后台 loop，持有传输层上下文。"""

    def __init__(self, cfg: "MCPServerConfig", aio_loop, call_timeout: int):
        self.cfg = cfg
        self._aio = aio_loop
        self._timeout = call_timeout
        self._session: ClientSession | None = None
        self._stop_evt: asyncio.Event | None = None
        self._task = None
        self._own_http: httpx2.AsyncClient | None = None

    def _ctx(self):
        if self.cfg.transport == "http":
            # SDK 2.x 无 headers 参数：鉴权等经自备 httpx2.AsyncClient 注入，自备自关
            self._own_http = httpx2.AsyncClient(headers=dict(self.cfg.headers or {}))
            return streamable_http_client(self.cfg.url, http_client=self._own_http)
        if self.cfg.transport == "sse":   # 旧版 SSE 协议（HTTP+SSE 双流，/sse 端点）
            return sse_client(self.cfg.url, headers=dict(self.cfg.headers or {}))
        params = StdioServerParameters(command=self.cfg.command,
                                       args=list(self.cfg.args or []),
                                       env=dict(self.cfg.env) if self.cfg.env else None)
        return stdio_client(params)

    async def _lifecycle(self, ready: concurrent.futures.Future):
        try:
            async with self._ctx() as streams:
                read_stream, write_stream = streams[0], streams[1]
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    self._session = session
                    self._stop_evt = asyncio.Event()
                    ready.set_result(tools)
                    await self._stop_evt.wait()
        except BaseException as e:   # 含 CancelledError：连接阶段任何失败如实上报
            if not ready.done():
                ready.set_exception(e)
        finally:
            self._session = None
            if self._own_http is not None:
                await self._own_http.aclose()
                self._own_http = None

    def start(self, connect_timeout: float = 30.0) -> list:
        ready = concurrent.futures.Future()
        self._task = asyncio.run_coroutine_threadsafe(self._lifecycle(ready), self._aio)
        return ready.result(connect_timeout)

    def call_tool(self, name: str, args: dict, timeout: int):
        session = self._session
        if session is None:
            raise RuntimeError("MCP 会话未就绪或已关闭（server 可能已掉线）")
        fut = asyncio.run_coroutine_threadsafe(session.call_tool(name, args or {}), self._aio)
        return fut.result(timeout)

    def stop(self, timeout: float = 10.0):
        evt = getattr(self, "_stop_evt", None)
        if evt is not None:
            self._aio.call_soon_threadsafe(evt.set)
        if self._task is not None:
            self._task.result(timeout)   # 等 __aexit__ 链走完（杀子进程/关连接）


class MCPManager:
    """全部 MCP server 的装配入口：单后台事件循环线程 + 注册/摘除注册表工具。"""

    def __init__(self, servers: dict[str, "MCPServerConfig"], call_timeout: int = 60):
        self._servers = dict(servers or {})
        self._timeout = call_timeout
        self._aio = None
        self._thread = None
        self._conns: dict[str, MCPConnection] = {}
        self._counts: dict[str, int] = {}

    def start_all(self) -> list[str]:
        """连接并注册全部 server，返回注册成功的工具名；单 server 失败不阻断。"""
        if not self._servers:
            return []
        self._aio = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._aio.run_forever,
                                        daemon=True, name="mcp-loop")
        self._thread.start()
        registered = []
        for name, cfg in self._servers.items():
            conn = MCPConnection(cfg, self._aio, self._timeout)
            try:
                tools = conn.start()
            except Exception as e:
                print(f"[agent] ⚠ MCP server {name} 连接失败: {type(e).__name__}: {e}")
                continue
            self._conns[name] = conn
            for t in tools:
                tool = MCPTool(name, t.name, t.description, t.input_schema,
                               make_call_bridge(conn, name, t.name, self._timeout))
                register_tool(tool, validate=False)
                registered.append(tool.name)
            self._counts[name] = len(tools)
        if not self._conns:
            self._shutdown_loop()   # 全失败：不留空转线程
        return registered

    def stop_all(self):
        """幂等：置位停止事件 → 等 __aexit__ 清理 → 停 loop 线程 → 摘除注册工具。"""
        for name, conn in self._conns.items():
            try:
                conn.stop()
            except Exception as e:
                print(f"[agent] ⚠ MCP server {name} 关闭异常: {type(e).__name__}: {e}")
        self._conns.clear()
        self._shutdown_loop()
        unregister_prefix(MCP_TOOL_PREFIX)

    def _shutdown_loop(self):
        if self._aio is not None:
            self._aio.call_soon_threadsafe(self._aio.stop)
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._aio = None
        self._thread = None

    def summary(self) -> str:
        return ", ".join(f"{n}({c} 工具)" for n, c in self._counts.items())
