# -*- coding: utf-8 -*-
# @File:     test_reliability.py
# @Author:   mjh
# @DateTime: 2026/03/15
"""P3 可靠性测试：token 预算、预算内正常完成、Ctrl+C 打断后会话保留。"""
import pytest

from agent.providers.base import LLMResponse, StopReason, ToolCall
from agent.loop import AgentLoop
from agent.permissions import PermissionGate
from test_loop import FakeProvider, BYPASS_GATE


def _tool_call(**arguments):
    return ToolCall(id="t1", name="Write", arguments=arguments)


# ---------- token 预算 ----------

def test_token_budget_terminates_task():
    big = LLMResponse(text=None,
                      tool_calls=[ToolCall(id="t", name="NotExist", arguments={})],
                      stop_reason=StopReason.TOOL_USE,
                      prompt_tokens=300000, completion_tokens=250000)

    class AlwaysTool:
        def chat(self, *a, **k):
            return big

    loop = AgentLoop(AlwaysTool(), max_turns=100, token_budget=500000,
                     permission_gate=BYPASS_GATE)
    out = loop.run("x")
    assert "[任务终止]" in out and "token 预算" in out
    assert "550000/500000" in out
    # 预算检查在工具执行之前：首个响应就超预算，本轮工具不执行
    assert len([m for m in loop.messages if m.role == "tool"]) == 0

def test_budget_not_triggered_when_within(ws, tmp_path):
    call = _tool_call(file_path=str(tmp_path / "a.txt"), content="x")
    script = [
        LLMResponse(text=None, tool_calls=[call], stop_reason=StopReason.TOOL_USE,
                    prompt_tokens=400, completion_tokens=100),
        LLMResponse(text="完成", tool_calls=[], stop_reason=StopReason.END_TURN,
                    prompt_tokens=400, completion_tokens=50),
    ]
    loop = AgentLoop(FakeProvider(script), max_turns=5, token_budget=1000,
                     permission_gate=BYPASS_GATE)
    # 两轮共 950 <= 1000，正常完成
    assert loop.run("x") == "完成"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "x"

def test_budget_counts_prompt_and_completion(tmp_path):
    # 预算按 prompt+completion 累计：第 1 轮后 600 <= 800 放行并执行工具，
    # 第 2 轮后 1200 > 800 触发终止（且第 2 轮的工具不执行）
    big = LLMResponse(text=None,
                      tool_calls=[ToolCall(id="t", name="NotExist", arguments={})],
                      stop_reason=StopReason.TOOL_USE,
                      prompt_tokens=300, completion_tokens=300)

    class AlwaysTool:
        def chat(self, *a, **k):
            return big

    loop = AgentLoop(AlwaysTool(), max_turns=100, token_budget=800,
                     permission_gate=BYPASS_GATE)
    out = loop.run("x")
    assert "[任务终止]" in out and "1200/800" in out
    assert len([m for m in loop.messages if m.role == "tool"]) == 1


# ---------- Ctrl+C 打断 ----------

def test_keyboardinterrupt_propagates_and_preserves_session(ws, tmp_path):
    call = _tool_call(file_path=str(tmp_path / "a.txt"), content="1")

    class InterruptOnSecondCall:
        def __init__(self):
            self.script = [LLMResponse(text=None, tool_calls=[call],
                                       stop_reason=StopReason.TOOL_USE)]

        def chat(self, *a, **k):
            if self.script:
                return self.script.pop(0)
            raise KeyboardInterrupt      # 模拟第二次等待模型时用户按下 Ctrl+C

    loop = AgentLoop(InterruptOnSecondCall(), max_turns=5,
                     permission_gate=BYPASS_GATE)
    with pytest.raises(KeyboardInterrupt):
        loop.run("x")
    # 打断后：历史完整保留（main 层接住 KI 后会话可继续），已执行的工具效果不回滚
    assert [m.role for m in loop.messages] == ["user", "assistant", "tool"]
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "1"

def test_session_continuable_after_interrupt(tmp_path):
    # 打断后再次 run()：旧历史保留，新任务正常完成
    call = _tool_call(file_path=str(tmp_path / "a.txt"), content="1")
    script = [
        LLMResponse(text=None, tool_calls=[call], stop_reason=StopReason.TOOL_USE),  # 任务1：调工具
        LLMResponse(text=None, tool_calls=[call], stop_reason=StopReason.TOOL_USE),  # 任务2：再调工具
        LLMResponse(text="第二个任务完成", tool_calls=[], stop_reason=StopReason.END_TURN),
    ]
    provider = FakeProvider(script)

    class InterruptOnSecondCall:
        def __init__(self):
            self.n = 0

        def chat(self, messages, tools=None, system=""):
            self.n += 1
            if self.n == 2:
                raise KeyboardInterrupt    # 任务1的工具结果回灌后、等待模型时打断
            return provider.chat(messages, tools, system)

    loop = AgentLoop(InterruptOnSecondCall(), max_turns=10,
                     permission_gate=BYPASS_GATE)
    with pytest.raises(KeyboardInterrupt):
        loop.run("第一个任务")

    assert loop.run("第二个任务") == "第二个任务完成"
    # 历史未被清空：两个任务的完整序列都在
    assert [m.role for m in loop.messages] == \
        ["user", "assistant", "tool",          # 任务1（被打断）
         "user", "assistant", "tool", "assistant"]  # 任务2（正常完成）
