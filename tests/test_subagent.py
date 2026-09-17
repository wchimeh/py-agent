# @File:     test_subagent.py
# @DateTime: 2026/09/17
"""AgentTool 子代理：Fake provider 装配，全程无网络。"""
import pytest

from agent.loop import AgentLoop
from agent.permissions import PermissionGate
from agent.providers.base import LLMResponse, StopReason, ToolCall
from agent.tools.base import ToolError

BYPASS_GATE = PermissionGate("bypass")


class FakeProvider:
    """按脚本依次返回预设响应；记录 tools/system 供断言。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen_tools = []
        self.seen_system = []

    def chat(self, messages, tools=None, system=""):
        self.seen_tools.append([t.name for t in (tools or [])])
        self.seen_system.append(system)
        return self.script.pop(0)


def _end_resp(text="子代理结论"):
    return LLMResponse(text=text, tool_calls=[], stop_reason=StopReason.END_TURN,
                       prompt_tokens=10, completion_tokens=5)


def _tool_resp(calls):
    return LLMResponse(text=None, tool_calls=calls, stop_reason=StopReason.TOOL_USE,
                       prompt_tokens=10, completion_tokens=2)


class FakeJournal:
    def __init__(self):
        self.events = []

    def log(self, event, **fields):
        self.events.append((event, fields))


@pytest.fixture(autouse=True)
def _reset():
    from agent.tools import subagent
    subagent.reset()
    yield
    subagent.reset()


def _configure(provider, **kw):
    from agent.tools import subagent
    subagent.configure(provider=provider, parent_gate=BYPASS_GATE,
                       max_turns=kw.get("max_turns", 10),
                       allow_bash=kw.get("allow_bash", False),
                       token_slice=kw.get("token_slice", 50000),
                       budget_pool=kw.get("budget_pool"),
                       journal=kw.get("journal"))


def test_unconfigured_friendly_error():
    from agent.tools.subagent import AgentTool
    with pytest.raises(ToolError, match="未启用"):
        AgentTool().execute(description="探查", prompt="总结仓库")


def test_runs_child_and_returns_report(ws):
    provider = FakeProvider([_end_resp("仓库共 3 个模块")])
    _configure(provider)
    from agent.tools.subagent import AgentTool
    out = AgentTool().execute(description="探查仓库", prompt="总结仓库结构")
    assert "[子代理·探查仓库]" in out
    assert "仓库共 3 个模块" in out


def test_child_tools_exclude_agent_and_journal_tagged(ws):
    provider = FakeProvider([_end_resp("done")])
    j = FakeJournal()
    _configure(provider, journal=j)
    from agent.tools.subagent import AgentTool
    AgentTool().execute(description="d", prompt="p")
    assert provider.seen_tools                  # 子循环确实发起了 LLM 调用
    assert all("Agent" not in names for names in provider.seen_tools)   # 防递归
    agents = {f.get("agent") for e, f in j.events}
    assert agents == {"sub-1"}                  # journal 全事件带子代理标识


def test_child_write_denied_by_subgate(ws, tmp_path):
    target = str(tmp_path / "no.txt")
    call = ToolCall(id="t1", name="Write",
                    arguments={"file_path": target, "content": "x"})
    provider = FakeProvider([_tool_resp([call]), _end_resp("无权写，报告结论")])
    _configure(provider)
    from agent.tools.subagent import AgentTool
    out = AgentTool().execute(description="d", prompt="p")
    import os
    assert not os.path.exists(target)           # SubGate 已接线：子代理写被拒
    assert "无权写" in out                      # 拒绝结果回灌后子代理照常收尾


def test_child_max_turns_termination_returns_text(ws):
    call = ToolCall(id="t", name="Read", arguments={"file_path": "x"})
    script = [_tool_resp([call]) for _ in range(20)]   # 永远要工具，超过 max_turns
    provider = FakeProvider(script)
    _configure(provider, max_turns=3)
    from agent.tools.subagent import AgentTool
    out = AgentTool().execute(description="d", prompt="p")
    assert "[任务终止]" in out and "上限" in out    # 终止文案也作为报告回灌，不抛异常


# ---------- TranscriptSink ----------

def test_transcript_sink_live_prefix_prints(capsys):
    from agent.tools.subagent import TranscriptSink
    sink = TranscriptSink("sub-1", live=True)
    sink.write("读取文件")
    out = capsys.readouterr().out
    assert "[sub-1] 读取文件" in out
    assert "读取文件" in sink.lines


def test_transcript_sink_quiet_no_stdout(capsys):
    from agent.tools.subagent import TranscriptSink
    sink = TranscriptSink("sub-2", live=False)
    sink.write("静默行")
    assert capsys.readouterr().out == ""        # 并发模式不占 stdout
    assert list(sink.lines) == ["静默行"]


def test_transcript_sink_bounded():
    from agent.tools.subagent import TranscriptSink
    sink = TranscriptSink("sub-3")
    for i in range(2500):
        sink.write(f"line{i}")
    assert len(sink.lines) == 2000              # 有界，防内存膨胀
    assert sink.lines[-1] == "line2499"


# ---------- config：subagent 节解析与校验 ----------

def test_config_subagent_defaults(tmp_path):
    from agent.config import load_config
    p = tmp_path / "config.yaml"
    p.write_text("", encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.subagent_max_turns == 15
    assert cfg.subagent_allow_bash is False
    assert cfg.subagent_token_slice == 100000


def test_config_subagent_parsed(tmp_path):
    from agent.config import load_config
    p = tmp_path / "config.yaml"
    p.write_text("subagent:\n  max_turns: 8\n  allow_bash: true\n"
                 "  token_slice: 50000\n", encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.subagent_max_turns == 8
    assert cfg.subagent_allow_bash is True
    assert cfg.subagent_token_slice == 50000


@pytest.mark.parametrize("bad", [
    "subagent:\n  max_turns: 0\n",
    "subagent:\n  token_slice: -1\n",
])
def test_config_subagent_validation_rejects(tmp_path, bad):
    from agent.config import load_config
    p = tmp_path / "config.yaml"
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(SystemExit, match="subagent"):
        load_config(str(p))


# ---------- M2：同回合多 Agent 并发执行 ----------

def test_same_turn_two_agents_run_concurrent_ordered(ws):
    import time

    class SlowProvider:
        def chat(self, messages, tools=None, system=""):
            time.sleep(0.6)
            return _end_resp("子代理结论")

    _configure(SlowProvider())
    calls = [ToolCall(id=f"a{i}", name="Agent",
                      arguments={"description": f"任务{i}", "prompt": "p"}) for i in range(2)]
    main = FakeProvider([_tool_resp(calls), _end_resp("主会话汇总")])
    loop = AgentLoop(main, permission_gate=BYPASS_GATE)
    t0 = time.time()
    out = loop.run("并发派发")
    elapsed = time.time() - t0
    assert out == "主会话汇总"
    assert elapsed < 1.1            # 串行 ≥1.2s；并发应 ~0.6s
    tool_msgs = [m for m in loop.messages if m.role == "tool"]
    assert len(tool_msgs) == 2
    assert "任务0" in tool_msgs[0].content    # 结果按原 tc 顺序回灌
    assert "任务1" in tool_msgs[1].content


def test_single_agent_turn_behavior_unchanged(ws):
    # 仅 1 个子代理类调用时不走线程池：行为与 M1 完全一致（同步串行）
    provider = FakeProvider([_end_resp("单代理结论")])
    _configure(provider)
    call = ToolCall(id="a0", name="Agent",
                    arguments={"description": "单任务", "prompt": "p"})
    main = FakeProvider([_tool_resp([call]), _end_resp("done")])
    loop = AgentLoop(main, permission_gate=BYPASS_GATE)
    assert loop.run("单派") == "done"
    tool_msgs = [m for m in loop.messages if m.role == "tool"]
    assert "单任务" in tool_msgs[0].content


def test_config_subagent_max_parallel(tmp_path):
    from agent.config import load_config
    p = tmp_path / "config.yaml"
    p.write_text("", encoding="utf-8")
    assert load_config(str(p)).subagent_max_parallel == 3   # 缺省 3
    p.write_text("subagent:\n  max_parallel: 5\n", encoding="utf-8")
    assert load_config(str(p)).subagent_max_parallel == 5
    p.write_text("subagent:\n  max_parallel: 0\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="subagent"):
        load_config(str(p))


def test_configure_sets_max_parallel():
    from agent.tools import subagent
    _configure(FakeProvider([]))
    assert subagent.MAX_PARALLEL == 3          # configure 未传时保持缺省
    subagent.configure(provider=FakeProvider([]), parent_gate=BYPASS_GATE,
                       max_parallel=5)
    assert subagent.MAX_PARALLEL == 5          # configure 注入并发上限


# ---------- M2：spawn ↔ SubagentRegistry 接线 ----------

def test_spawn_registers_in_registry(ws):
    from agent.tools import subagent
    from agent.tools.subagent import AgentTool
    from agent.viewer import SubagentRegistry
    r = SubagentRegistry()
    call = ToolCall(id="t1", name="Read", arguments={"file_path": "x"})
    provider = FakeProvider([_tool_resp([call]), _end_resp("报告完成")])
    subagent.configure(provider=provider, parent_gate=BYPASS_GATE, registry=r)
    AgentTool().execute(description="探查仓库", prompt="p")
    snap = r.snapshot()
    assert snap[0]["id"] == "sub-1" and snap[0]["status"] == "done"
    assert any("● Read(" in ln for ln in r.get_lines("sub-1"))   # transcript 进 registry


def test_spawn_stopped_status_on_termination(ws):
    from agent.tools import subagent
    from agent.tools.subagent import AgentTool
    from agent.viewer import SubagentRegistry
    r = SubagentRegistry()
    call = ToolCall(id="t", name="Read", arguments={"file_path": "x"})
    provider = FakeProvider([_tool_resp([call]) for _ in range(20)])
    subagent.configure(provider=provider, parent_gate=BYPASS_GATE, registry=r,
                       max_turns=3)
    AgentTool().execute(description="d", prompt="p")
    assert r.snapshot()[0]["status"] == "stopped"    # 终止=stopped 区别于 done
