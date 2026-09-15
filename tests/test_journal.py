# -*- coding: utf-8 -*-
# @File:     test_journal.py
# @Author:   mjh
# @DateTime: 2026/03/15
"""Journal 测试：JSONL 追加、禁用无 IO、daily 路径；loop 埋点事件序列。"""
import json
import os
from datetime import datetime

from agent.providers.base import LLMResponse, StopReason, ToolCall
from agent.loop import AgentLoop
from agent.journal import Journal
from agent.permissions import PermissionGate
from test_loop import FakeProvider, BYPASS_GATE


def read_events(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _tool_resp(call, prompt=10, completion=5):
    return LLMResponse(text=None, tool_calls=[call], stop_reason=StopReason.TOOL_USE,
                       prompt_tokens=prompt, completion_tokens=completion)


def _end_resp(text="完成"):
    return LLMResponse(text=text, tool_calls=[], stop_reason=StopReason.END_TURN,
                       prompt_tokens=10, completion_tokens=5)


# ---------- Journal 本体 ----------

def test_journal_appends_jsonl(tmp_path):
    p = str(tmp_path / "j.jsonl")
    j = Journal(p)
    j.log("task_start", input="hi")
    j.log("llm_call", prompt=10, completion=5, secs=0.1, stop="end_turn")
    events = read_events(p)
    assert len(events) == 2
    assert events[0]["event"] == "task_start" and events[0]["input"] == "hi"
    assert "ts" in events[0]                              # 每行带时间戳
    assert events[1]["stop"] == "end_turn" and events[1]["prompt"] == 10


def test_journal_disabled_writes_nothing(tmp_path):
    Journal(None).log("x", y=1)                           # 不抛
    assert os.listdir(tmp_path) == []


def test_journal_daily_creates_dated_file(tmp_path):
    j = Journal.daily(str(tmp_path))
    j.log("e")
    files = os.listdir(tmp_path)
    assert files == [f"{datetime.now():%Y%m%d}.jsonl"]


# ---------- loop 埋点 ----------

def test_loop_journal_event_sequence(tmp_path):
    path = str(tmp_path / "j.jsonl")
    call = ToolCall(id="t1", name="Write",
                    arguments={"file_path": str(tmp_path / "a.txt"), "content": "x"})
    provider = FakeProvider([_tool_resp(call), _end_resp()])
    loop = AgentLoop(provider, max_turns=5, permission_gate=BYPASS_GATE,
                     journal=Journal(path))

    assert loop.run("任务") == "完成"
    assert [e["event"] for e in read_events(path)] == \
        ["task_start", "llm_call", "tool_call", "llm_call", "task_end"]
    # 统计计数器与事件一致
    assert loop.tasks_total == 1
    assert loop.tool_calls == {"Write": 1}


def test_loop_logs_denied_not_counted_as_tool(tmp_path):
    path = str(tmp_path / "j.jsonl")
    gate = PermissionGate("default", ask_fn=lambda tc, danger: "n")
    call = ToolCall(id="t1", name="Write", arguments={"file_path": "a", "content": "x"})
    provider = FakeProvider([_tool_resp(call), _end_resp("被拒")])
    loop = AgentLoop(provider, max_turns=5, permission_gate=gate,
                     journal=Journal(path))

    loop.run("x")
    events = read_events(path)
    assert any(e["event"] == "denied" and e["tool"] == "Write" for e in events)
    assert loop.tool_calls == {}                          # 拒绝不计入工具统计


def test_loop_logs_compact(tmp_path):
    path = str(tmp_path / "j.jsonl")
    call = ToolCall(id="t1", name="Write",
                    arguments={"file_path": str(tmp_path / "a.txt"), "content": "x"})
    provider = FakeProvider([
        _tool_resp(call, prompt=900),
        LLMResponse(text="摘要", tool_calls=[], stop_reason=StopReason.END_TURN,
                    prompt_tokens=800, completion_tokens=10),
        _end_resp()])
    loop = AgentLoop(provider, max_turns=5, permission_gate=BYPASS_GATE,
                     context_window=1000, compact_threshold=0.8, keep_recent=2,
                     journal=Journal(path))

    loop.run("x")
    compacts = [e for e in read_events(path) if e["event"] == "compact"]
    assert len(compacts) == 1
    # 短历史下压缩是 1:1 置换（1 条 user → 1 条摘要）：条数持平、token 下降
    assert compacts[0]["msgs_before"] == 3 and compacts[0]["msgs_after"] == 3
    assert compacts[0]["degraded"] is False


def test_loop_without_journal_writes_no_files(tmp_path, monkeypatch):
    # journal=None 默认：跑完任务后工作目录无任何新文件
    monkeypatch.chdir(tmp_path)
    provider = FakeProvider([_end_resp("直接回答")])
    loop = AgentLoop(provider, permission_gate=BYPASS_GATE)
    assert loop.run("x") == "直接回答"
    assert os.listdir(tmp_path) == []
