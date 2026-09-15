# -*- coding: utf-8 -*-
# @File:     test_loop_compact.py
# @Author:   mjh
# @DateTime: 2026/03/15
"""P5 集成测试：超阈值自动压缩、历史变短、降级截断、未超零副作用。"""
from agent.providers.base import LLMResponse, StopReason, ToolCall
from agent.providers.retry import AgentError
from agent.loop import AgentLoop
from test_loop import BYPASS_GATE


class ScriptProvider:
    """脚本化响应 + 记录每次请求（消息条数, 是否摘要调用），脚本项为异常时抛出。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []  # (len(messages), tools_is_none)

    def chat(self, messages, tools=None, system=""):
        self.calls.append((len(messages), tools is None))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _write_call(path, i):
    return ToolCall(id=f"t{i}", name="Write",
                    arguments={"file_path": path, "content": "x"})


def _tool_resp(call, prompt=10, completion=5):
    return LLMResponse(text=None, tool_calls=[call], stop_reason=StopReason.TOOL_USE,
                       prompt_tokens=prompt, completion_tokens=completion)


def _end_resp(text="完成", prompt=10, completion=5):
    return LLMResponse(text=text, tool_calls=[], stop_reason=StopReason.END_TURN,
                       prompt_tokens=prompt, completion_tokens=completion)


_SUMMARY = LLMResponse(text="摘要：用户要求写文件，已完成第一步。", tool_calls=[],
                       stop_reason=StopReason.END_TURN,
                       prompt_tokens=800, completion_tokens=100)


def test_compact_triggers_and_shrinks_history(ws, tmp_path):
    # window=1000 阈值 800：第 2 轮锚点 900 → 第 3 轮前触发压缩
    f1, f2 = str(tmp_path / "1.txt"), str(tmp_path / "2.txt")
    provider = ScriptProvider([
        _tool_resp(_write_call(f1, 1), prompt=10),
        _tool_resp(_write_call(f2, 2), prompt=900),   # 大锚点 → 下一轮超阈值
        _SUMMARY,
        _end_resp("任务完成"),
    ])
    loop = AgentLoop(provider, max_turns=10, permission_gate=BYPASS_GATE,
                     context_window=1000, compact_threshold=0.8, keep_recent=2)

    assert loop.run("写两个文件") == "任务完成"
    assert provider.calls == [(1, False), (3, False), (1, True), (3, False)]
    # ^ 第3次是摘要请求（单条消息+无工具）；第4次历史 5→3 条（变短）
    assert loop.compact_count == 1
    assert loop.messages[0].content.startswith("[会话摘要]")
    assert loop.messages[0].content == "[会话摘要]\n" + _SUMMARY.text
    # 工具真实执行过，压缩不回滚副作用
    assert open(f1, encoding="utf-8").read() == "x"
    assert open(f2, encoding="utf-8").read() == "x"

def test_summary_usage_counted_in_stats(ws, tmp_path):
    provider = ScriptProvider([
        _tool_resp(_write_call(str(tmp_path / "a.txt"), 1), prompt=900),
        _SUMMARY,                                        # 800/100 计入统计
        _end_resp(),
    ])
    loop = AgentLoop(provider, max_turns=5, permission_gate=BYPASS_GATE,
                     context_window=1000, compact_threshold=0.8, keep_recent=2)
    loop.run("x")
    assert loop.total_prompt_tokens == 900 + 800 + 10
    assert loop.total_completion_tokens == 5 + 100 + 5

def test_summary_failure_degrades_to_truncation(ws, tmp_path):
    # 摘要调用抛 AgentError（重试耗尽后的真实形态）→ 降级截断，任务继续完成
    provider = ScriptProvider([
        _tool_resp(_write_call(str(tmp_path / "a.txt"), 1), prompt=900),
        AgentError("API 不可用", retryable=False),
        _end_resp("降级后完成"),
    ])
    loop = AgentLoop(provider, max_turns=5, permission_gate=BYPASS_GATE,
                     context_window=1000, compact_threshold=0.8, keep_recent=2)

    assert loop.run("x") == "降级后完成"
    assert loop.compact_count == 1
    assert "[会话摘要·截断]" in loop.messages[0].content

def test_no_compact_when_within_threshold(ws, tmp_path):
    # 估算远低于阈值：不触发压缩，provider 调用次数与 P4 完全一致（无隐藏成本）
    provider = ScriptProvider([
        _tool_resp(_write_call(str(tmp_path / "a.txt"), 1), prompt=50),
        _end_resp(),
    ])
    loop = AgentLoop(provider, max_turns=5, permission_gate=BYPASS_GATE,
                     context_window=100000, compact_threshold=0.8, keep_recent=8)
    loop.run("x")
    assert provider.calls == [(1, False), (3, False)]   # 没有任何摘要调用
    assert loop.compact_count == 0

def test_context_window_zero_disables_compact(ws, tmp_path):
    provider = ScriptProvider([
        _tool_resp(_write_call(str(tmp_path / "a.txt"), 1), prompt=999999),
        _end_resp(),
    ])
    loop = AgentLoop(provider, max_turns=5, permission_gate=BYPASS_GATE,
                     context_window=0)
    loop.run("x")
    assert loop.compact_count == 0                      # 0 = 显式关闭

def test_compacted_messages_keep_tool_pairing(ws, tmp_path):
    # 压缩后 kept 部分满足 anthropic 配对约束，可直接作为下一轮请求历史
    tc = ToolCall(id="t9", name="Write",
                  arguments={"file_path": str(tmp_path / "a.txt"), "content": "x"})
    provider = ScriptProvider([
        LLMResponse(text=None, tool_calls=[tc], stop_reason=StopReason.TOOL_USE,
                    prompt_tokens=900, completion_tokens=5),
        _SUMMARY,
        _end_resp(),
    ])
    loop = AgentLoop(provider, max_turns=5, permission_gate=BYPASS_GATE,
                     context_window=1000, compact_threshold=0.8, keep_recent=2)
    loop.run("x")

    uses, results = set(), set()
    for m in loop.messages:
        if m.role == "assistant":
            uses.update(c.id for c in m.tool_calls)
        elif m.role == "tool":
            results.add(m.tool_call_id)
    assert uses == results
