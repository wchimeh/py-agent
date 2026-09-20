# @File:     test_mcp.py
# @Author:   mjh
# @DateTime: 2026/09/20
"""MCP 客户端单测：注册表动态化 / MCPTool / 同步桥接 / 配置解析。除 stdio 回路外零网络零子进程。"""
import sys
import threading
from types import SimpleNamespace

import pytest

import agent.mcp_client as mcp_mod
from agent.config import MCPServerConfig, load_config
from agent.mcp_client import MCPManager, MCPTool, flatten_content, make_call_bridge
from agent.providers.base import ToolCall
from agent.tools.base import Tool, ToolError
from agent.tools.registry import (
    _TOOLS,
    check_tool_call,
    execute_tool,
    get_tool_defs,
    register_tool,
    unregister_prefix,
)


def _server_cfg(**over):
    cfg = dict(transport="stdio", command="fake", args=[], env=None, url="", headers={})
    cfg.update(over)
    return SimpleNamespace(**cfg)


class _DynTool(Tool):
    """动态注册测试替身：捕获 execute 收到的原始 kwargs。"""

    def __init__(self, name, schema=None):
        self.name = name
        self.description = "动态测试工具"
        self.parameters = schema or {"type": "object",
                                     "properties": {"a": {"type": "string"}},
                                     "required": ["a"]}
        self.captured = None

    def execute(self, **kwargs):
        self.captured = dict(kwargs)
        return "ok"


@pytest.fixture(autouse=True)
def _cleanup_dynamic():
    yield
    unregister_prefix("__dyn_")


# ---------- M1：注册表动态化 ----------

def test_register_tool_passthrough_skips_validation():
    tool = _DynTool("__dyn_mcp__echo")
    register_tool(tool, validate=False)
    tc = ToolCall(id="1", name="__dyn_mcp__echo",
                  arguments={"a": 1, "extra": [1, 2]})   # 类型不符 + 未知参数
    assert check_tool_call(tc) is None                    # 透传：本地不拦
    text, is_err = execute_tool(tc)
    assert (text, is_err) == ("ok", False)
    assert tool.captured == {"a": 1, "extra": [1, 2]}     # 原样到达 execute


def test_register_tool_validate_enforces_schema():
    tool = _DynTool("__dyn_strict")
    register_tool(tool, validate=True)
    tc = ToolCall(id="1", name="__dyn_strict", arguments={"a": 1, "extra": "x"})
    err = check_tool_call(tc)
    assert err is not None and "参数校验失败" in err


def test_unregister_prefix_removes_only_matching():
    mcp_a = _DynTool("__dyn_mcp__a")
    mcp_b = _DynTool("__dyn_mcp__b")
    inner = _DynTool("__dyn_inner")
    for t in (mcp_a, mcp_b, inner):
        register_tool(t, validate=False)
    removed = unregister_prefix("__dyn_mcp__")
    assert removed == 2
    assert "__dyn_mcp__a" not in _TOOLS and "__dyn_mcp__b" not in _TOOLS
    assert "__dyn_inner" in _TOOLS                        # 前缀外的原样保留
    names = [d.name for d in get_tool_defs()]
    assert "Read" in names                                # 内置不受影响


def test_register_tool_overwrites_same_name():
    register_tool(_DynTool("__dyn_dup"), validate=False)
    second = _DynTool("__dyn_dup")
    register_tool(second, validate=False)
    assert _TOOLS["__dyn_dup"] is second                  # 同名后注册覆盖


# ---------- M2：MCPTool 纯逻辑 ----------

class _FakeConn:
    """桥接层替身：call_tool 可编程返回/抛错。"""

    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    def call_tool(self, name, args, timeout):
        self.calls.append((name, args, timeout))
        if self.exc:
            raise self.exc
        return self.result


def _res(blocks, is_error=False):
    return SimpleNamespace(content=blocks, isError=is_error)


def test_flatten_content_text_blocks_joined():
    out = flatten_content([SimpleNamespace(text="part1"), SimpleNamespace(text="part2")])
    assert out == "part1\npart2"


def test_flatten_content_binary_placeholder():
    img = SimpleNamespace(data="b64", mimeType="image/png")
    out = flatten_content([SimpleNamespace(text="hi"), img])
    assert out.startswith("hi\n")
    assert "不回灌" in out


def test_flatten_content_empty():
    assert flatten_content(None) == "(无内容)"
    assert flatten_content([]) == "(无内容)"


def test_mcp_tool_name_prefix_and_defaults():
    tool = MCPTool("fs", "read_file", None, None, lambda kw: "x")
    assert tool.name == "mcp__fs__read_file"
    assert tool.description == "MCP server fs 提供的工具"
    assert tool.parameters == {"type": "object", "properties": {}}


def test_mcp_tool_passes_kwargs_to_call():
    seen = {}
    MCPTool("fs", "t", "d", {}, lambda kw: seen.update(kw) or "ok").execute(a=1, b="x")
    assert seen == {"a": 1, "b": "x"}


def test_bridge_returns_flattened_text():
    conn = _FakeConn(_res([SimpleNamespace(text="hello")]))
    call = make_call_bridge(conn, "fs", "echo", timeout=5)
    assert call({"q": 1}) == "hello"
    assert conn.calls == [("echo", {"q": 1}, 5)]


def test_bridge_is_error_raises_tool_error():
    conn = _FakeConn(_res([SimpleNamespace(text="boom")], is_error=True))
    call = make_call_bridge(conn, "fs", "echo", timeout=5)
    with pytest.raises(ToolError, match="boom"):
        call({})


def test_bridge_timeout_mentions_server_and_seconds():
    import concurrent.futures
    conn = _FakeConn(exc=concurrent.futures.TimeoutError())
    call = make_call_bridge(conn, "fs", "echo", timeout=7)
    with pytest.raises(ToolError, match=r"fs.*7"):
        call({})


def test_bridge_generic_exception_wrapped():
    conn = _FakeConn(exc=RuntimeError("conn closed"))
    call = make_call_bridge(conn, "fs", "echo", timeout=5)
    with pytest.raises(ToolError, match=r"fs.*RuntimeError.*conn closed"):
        call({})


def test_bridge_truncates_long_output():
    conn = _FakeConn(_res([SimpleNamespace(text="A" * 40000)]))
    call = make_call_bridge(conn, "fs", "echo", timeout=5)
    out = call({})
    assert len(out) < 25000 and "已截断" in out


# ---------- M2：桥接生命周期与编排 ----------

class _FakeConnection:
    """替代 MCPConnection：可编程的工具清单与调用结果。"""

    def __init__(self, cfg, aio_loop, call_timeout):
        self.cfg = cfg
        self.calls = []

    def start(self, connect_timeout=30.0):
        spec = SERVER_SPECS[self.cfg.command]
        if isinstance(spec, Exception):
            raise spec
        return [SimpleNamespace(name=n, description=f"{n} 工具",
                                input_schema={"type": "object", "properties": {}})
                for n in spec]

    def call_tool(self, name, args, timeout):
        self.calls.append((name, args))
        return _res([SimpleNamespace(text=f"{name}:{args.get('msg', '')}")])

    def stop(self, timeout=10.0):
        pass


SERVER_SPECS: dict = {}


@pytest.fixture
def fake_conn(monkeypatch):
    monkeypatch.setattr(mcp_mod, "MCPConnection", _FakeConnection)
    SERVER_SPECS.clear()
    SERVER_SPECS["fake"] = ["echo", "upper"]
    yield SERVER_SPECS
    SERVER_SPECS.clear()


def test_manager_registers_tools_and_stop_cleans_up(fake_conn):
    mgr = MCPManager({"fs": _server_cfg()}, call_timeout=5)
    names = mgr.start_all()
    assert set(names) == {"mcp__fs__echo", "mcp__fs__upper"}
    assert any(t.name == "mcp__fs__echo" for t in get_tool_defs())
    assert any(t.name == "mcp-loop" for t in threading.enumerate())   # loop 线程在跑
    text, is_err = execute_tool(ToolCall(id="1", name="mcp__fs__echo", arguments={"msg": "hi"}))
    assert (text, is_err) == ("echo:hi", False)
    mgr.stop_all()
    assert not any(t.name == "mcp-loop" for t in threading.enumerate())   # 无线程残留
    assert not [n for n in _TOOLS if n.startswith("mcp__")]              # 工具已摘除


def test_manager_single_server_failure_does_not_block(fake_conn, capsys):
    SERVER_SPECS["bad"] = RuntimeError("spawn failed")
    mgr = MCPManager({"bad": _server_cfg(command="bad"), "good": _server_cfg(command="fake")},
                     call_timeout=5)
    names = mgr.start_all()
    assert set(names) == {"mcp__good__echo", "mcp__good__upper"}
    assert "bad" in capsys.readouterr().out                            # 警告如实打印
    mgr.stop_all()


def test_manager_stop_all_idempotent(fake_conn):
    mgr = MCPManager({"fs": _server_cfg()}, call_timeout=5)
    mgr.start_all()
    mgr.stop_all()
    mgr.stop_all()                                                     # 二次调用不抛


def test_manager_empty_servers_starts_nothing():
    mgr = MCPManager({}, call_timeout=5)
    assert mgr.start_all() == []
    assert mgr._thread is None and mgr._aio is None
    mgr.stop_all()                                                     # 空转安全


def test_connection_failure_propagates(monkeypatch):
    import asyncio

    def bad_ctx(self):
        raise RuntimeError("connect refused")

    monkeypatch.setattr(mcp_mod.MCPConnection, "_ctx", bad_ctx)
    aio = asyncio.new_event_loop()
    t = threading.Thread(target=aio.run_forever, daemon=True)
    t.start()
    try:
        conn = mcp_mod.MCPConnection(_server_cfg(), aio, 5)
        with pytest.raises(RuntimeError, match="connect refused"):
            conn.start(connect_timeout=5)
    finally:
        aio.call_soon_threadsafe(aio.stop)
        t.join(timeout=5)


# ---------- M2：真 SDK 协议回路（内存传输，官方测试设施） ----------

def test_real_sdk_memory_roundtrip(monkeypatch):
    pytest.importorskip("mcp.client._memory")
    from mcp.client._memory import InMemoryTransport
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("mem")

    def echo(msg: str) -> str:
        return f"echo:{msg}"

    server.add_tool(echo)

    monkeypatch.setattr(mcp_mod.MCPConnection, "_ctx",
                        lambda self: InMemoryTransport(server))
    mgr = MCPManager({"mem": _server_cfg()}, call_timeout=10)
    assert mgr.start_all() == ["mcp__mem__echo"]
    text, is_err = execute_tool(ToolCall(id="1", name="mcp__mem__echo", arguments={"msg": "真回路"}))
    assert (text, is_err) == ("echo:真回路", False)
    mgr.stop_all()
    assert "mcp__mem__echo" not in _TOOLS


def test_real_stdio_subprocess_roundtrip(tmp_path):
    """全链路真子进程：manager 起脚本 → initialize → list_tools → call_tool → stop 杀进程。"""
    script = tmp_path / "echo_server.py"
    script.write_text(
        "from mcp.server.mcpserver import MCPServer\n"
        "server = MCPServer('echo-stdio')\n"
        "def echo(msg: str) -> str:\n"
        "    return f'stdio:{msg}'\n"
        "server.add_tool(echo)\n"
        "server.run('stdio')\n",
        encoding="utf-8")
    mgr = MCPManager({"s": _server_cfg(command=sys.executable, args=[str(script)])},
                     call_timeout=30)
    assert mgr.start_all() == ["mcp__s__echo"]
    text, is_err = execute_tool(ToolCall(id="1", name="mcp__s__echo",
                                         arguments={"msg": "子进程"}))
    assert (text, is_err) == ("stdio:子进程", False)
    mgr.stop_all()
    assert "mcp__s__echo" not in _TOOLS
    assert not any(t.name == "mcp-loop" for t in threading.enumerate())


# ---------- M3：config mcp 段 ----------

def _load(tmp_path, text):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(text, encoding="utf-8")
    import os
    old = os.getcwd()
    os.chdir(tmp_path)   # load_config 相对路径默认 config.yaml
    try:
        return load_config(str(cfg_file))
    finally:
        os.chdir(old)


_STDIO_YAML = """
mcp:
  call_timeout: 30
  servers:
    fs:
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
      env:
        FOO: bar
"""

_HTTP_YAML = """
mcp:
  servers:
    remote:
      transport: http
      url: https://example.com/mcp
      headers:
        Authorization: "Bearer t"
"""


def test_config_stdio_server_parsed(tmp_path):
    cfg = _load(tmp_path, _STDIO_YAML)
    assert cfg.mcp_call_timeout == 30
    fs = cfg.mcp_servers["fs"]
    assert isinstance(fs, MCPServerConfig)
    assert (fs.transport, fs.command, fs.args, fs.env) == \
        ("stdio", "npx", ["-y", "@modelcontextprotocol/server-filesystem", "."], {"FOO": "bar"})
    assert fs.url == "" and fs.headers == {}


def test_config_http_server_parsed(tmp_path):
    cfg = _load(tmp_path, _HTTP_YAML)
    assert cfg.mcp_call_timeout == 60                      # 缺省
    r = cfg.mcp_servers["remote"]
    assert (r.transport, r.url) == ("http", "https://example.com/mcp")
    assert r.headers == {"Authorization": "Bearer t"}
    assert r.command == ""                                  # stdio 专属字段缺省空


def test_config_no_mcp_section_defaults(tmp_path):
    cfg = _load(tmp_path, "provider: openai\n")
    assert cfg.mcp_servers == {}
    assert cfg.mcp_call_timeout == 60


def test_config_rejects_bad_transport(tmp_path):
    with pytest.raises(SystemExit, match="transport"):
        _load(tmp_path, "mcp:\n  servers:\n    x:\n      transport: ftp\n")


def test_config_rejects_stdio_without_command(tmp_path):
    with pytest.raises(SystemExit, match="command"):
        _load(tmp_path, "mcp:\n  servers:\n    x:\n      transport: stdio\n")


def test_config_rejects_http_without_url(tmp_path):
    with pytest.raises(SystemExit, match="url"):
        _load(tmp_path, "mcp:\n  servers:\n    x:\n      transport: http\n")


def test_config_rejects_non_http_url(tmp_path):
    with pytest.raises(SystemExit, match="url"):
        _load(tmp_path, "mcp:\n  servers:\n    x:\n      transport: http\n      url: ftp://x\n")


def test_config_rejects_bad_call_timeout(tmp_path):
    with pytest.raises(SystemExit, match="call_timeout"):
        _load(tmp_path, "mcp:\n  call_timeout: 0\n")


# ---------- 权限门交互（既有分支的 MCP 专属守卫） ----------

def test_mcp_tool_asks_conservatively_in_default_mode():
    from agent.permissions import PermissionGate

    asked = []

    def ask(tc, danger):
        asked.append(tc.name)
        return "y"

    gate = PermissionGate("default", ask_fn=ask)
    ok, _ = gate.authorize(ToolCall(id="1", name="mcp__fs__list", arguments={}))
    assert ok is True and asked == ["mcp__fs__list"]   # 未知工具保守询问分支


def test_subgate_denies_mcp_tool():
    from agent.permissions import PermissionGate, SubGate

    sub = SubGate(PermissionGate("bypass"))
    ok, _ = sub.authorize(ToolCall(id="1", name="mcp__fs__list", arguments={}))
    assert ok is False   # 子代理白名单外一律拒：MCP 工具不进子代理


# ---------- M5：SSE 传输 + 配置笔误防静默 ----------

def test_config_sse_server_parsed(tmp_path):
    cfg = _load(tmp_path, """
mcp:
  servers:
    remote:
      transport: sse
      url: http://127.0.0.1:2345/sse
      headers:
        Authorization: "Bearer t"
""")
    r = cfg.mcp_servers["remote"]
    assert (r.transport, r.url) == ("sse", "http://127.0.0.1:2345/sse")
    assert r.headers == {"Authorization": "Bearer t"}


def test_config_rejects_unknown_mcp_key(tmp_path):
    # 笔误场景：server 名写成 mcp 顶级键（真机踩坑：静默吞掉导致"连不上且无报错"）
    with pytest.raises(SystemExit, match="remote"):
        _load(tmp_path, """
mcp:
  servers:
  remote:
    transport: http
    url: http://x/mcp
""")


def test_config_rejects_sse_without_url(tmp_path):
    with pytest.raises(SystemExit, match="url"):
        _load(tmp_path, "mcp:\n  servers:\n    x:\n      transport: sse\n")


def test_sse_ctx_passes_url_and_headers(monkeypatch):
    captured = {}

    def fake_sse_client(url, headers=None, **kw):
        captured["url"], captured["headers"] = url, headers
        return "CTX"

    monkeypatch.setattr(mcp_mod, "sse_client", fake_sse_client)
    conn = mcp_mod.MCPConnection(_server_cfg(transport="sse",
                                              url="http://h/sse",
                                              headers={"Authorization": "Bearer t"}),
                                 None, 60)
    assert conn._ctx() == "CTX"
    assert captured == {"url": "http://h/sse", "headers": {"Authorization": "Bearer t"}}
