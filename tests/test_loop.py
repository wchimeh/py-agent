# @File:     test_loop.py
# @Author:   mjh
# @DateTime: 2026/03/14
"""AgentLoop 工具往返测试：FakeProvider 脚本化响应，全程无网络。"""
import os

from agent.loop import AgentLoop
from agent.permissions import PermissionGate
from agent.providers.base import LLMResponse, StopReason, ToolCall

# bypass 模式 authorize 直接放行、不弹询问、无状态变更，可安全共享
BYPASS_GATE = PermissionGate("bypass")


class FakeProvider:
    """按脚本依次返回预设响应"""

    def __init__(self, script):
        self.script = list(script)

    def chat(self, messages, tools=None, system=""):
        return self.script.pop(0)


class LoopForever:
    """永远请求工具，用于验证 max_turns 防线"""

    def chat(self, messages, tools=None, system=""):
        return LLMResponse(
            text=None,
            tool_calls=[ToolCall(id="t", name="NotExist", arguments={})],
            stop_reason=StopReason.TOOL_USE)


def _tool_resp(calls):
    return LLMResponse(text=None, tool_calls=calls, stop_reason=StopReason.TOOL_USE)


def _end_resp(text="完成"):
    return LLMResponse(text=text, tool_calls=[], stop_reason=StopReason.END_TURN,
                       prompt_tokens=10, completion_tokens=5)


def test_roundtrip_executes_tool_and_finishes(ws, tmp_path):
    target = str(tmp_path / "a.txt")
    call = ToolCall(id="t1", name="Write",
                    arguments={"file_path": target, "content": "hello"})
    provider = FakeProvider([_tool_resp([call]), _end_resp("写入完成")])
    loop = AgentLoop(provider, max_turns=5, permission_gate=BYPASS_GATE)

    assert loop.run("写文件") == "写入完成"
    with open(target, encoding="utf-8") as f:
        assert f.read() == "hello"
    # 消息序列：user → assistant(tool_use) → tool → assistant(text)
    assert [m.role for m in loop.messages] == ["user", "assistant", "tool", "assistant"]


def test_journal_task_start_records_prompt_version():
    class FakeJournal:
        def __init__(self):
            self.events = []

        def log(self, event, **fields):
            self.events.append((event, fields))

    j = FakeJournal()
    loop = AgentLoop(FakeProvider([_end_resp()]), permission_gate=BYPASS_GATE, journal=j)
    loop.run("hi")
    start = next(f for e, f in j.events if e == "task_start")
    assert start["prompt"] == 3  # 默认最新版 system_v3（P17 M1 子代理指引版）
    # P14：llm_call 的 token 数字段改名 prompt_tokens，prompt 只保留版本语义
    llm = next(f for e, f in j.events if e == "llm_call")
    assert llm["prompt_tokens"] == 10 and "prompt" not in llm


def test_tool_call_line_arg_display(ws, tmp_path, capsys):
    # P14：短值（≤60 字符）完整显示；超长值截断并带 (len=N)；参数显示上限 3
    long_path = str(tmp_path / ("x" * 80 + ".txt"))
    provider = FakeProvider([
        _tool_resp([ToolCall(id="t1", name="Edit", arguments={
            "file_path": long_path, "old_string": "OLD",
            "new_string": "NEW", "replace_all": False})]),
        _end_resp()])
    AgentLoop(provider, max_turns=5, permission_gate=BYPASS_GATE).run("编辑")
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("● Edit("))
    assert "'OLD'" in line                       # 短值完整显示
    assert long_path not in line                 # 超长路径被截断
    assert "(len=" in line                       # 截断值带长度标注
    assert "replace_all" not in line             # 参数显示上限 3


class RecordingGate:
    """记录 authorize 调用；合法/非法判定无关，只关心是否被触发"""

    def __init__(self):
        self.calls = []

    def authorize(self, tc):
        self.calls.append(tc.name)
        return True, ""


def test_malformed_tool_call_skips_permission_gate():
    from agent.providers.base import MALFORMED_ARGS_KEY
    gate = RecordingGate()
    call = ToolCall(id="t1", name="Bash", arguments={MALFORMED_ARGS_KEY: "not json"})
    loop = AgentLoop(FakeProvider([_tool_resp([call]), _end_resp()]), permission_gate=gate)
    loop.run("hi")
    assert gate.calls == []          # 畸形调用不弹权限
    assert any(m.role == "tool" and m.is_error for m in loop.messages)

def test_unknown_tool_skips_permission_gate():
    gate = RecordingGate()
    call = ToolCall(id="t1", name="NotExist", arguments={})
    loop = AgentLoop(FakeProvider([_tool_resp([call]), _end_resp()]), permission_gate=gate)
    loop.run("hi")
    assert gate.calls == []          # 未知工具不弹权限，直接错误回灌


def test_multiple_tool_calls_all_answered(ws, tmp_path):
    f1, f2 = str(tmp_path / "1.txt"), str(tmp_path / "2.txt")
    calls = [ToolCall(id="t1", name="Write",
                      arguments={"file_path": f1, "content": "one"}),
             ToolCall(id="t2", name="Write",
                      arguments={"file_path": f2, "content": "two"})]
    provider = FakeProvider([_tool_resp(calls), _end_resp()])
    loop = AgentLoop(provider, max_turns=5, permission_gate=BYPASS_GATE)

    loop.run("写两个文件")
    # 两个 tool_call 必须各有一条 tool_result 回灌
    assert [m.role for m in loop.messages] == \
        ["user", "assistant", "tool", "tool", "assistant"]
    with open(f1, encoding="utf-8") as f:
        assert f.read() == "one"
    with open(f2, encoding="utf-8") as f:
        assert f.read() == "two"


def test_unknown_tool_error_fed_back_not_crash():
    call = ToolCall(id="t1", name="NotExist", arguments={})
    provider = FakeProvider([_tool_resp([call]), _end_resp("好的")])
    loop = AgentLoop(provider, max_turns=5, permission_gate=BYPASS_GATE)

    out = loop.run("x")
    assert out == "好的"
    tr = loop.messages[2]
    assert tr.is_error and "未知工具" in tr.content


def test_tool_exception_fed_back(tmp_path):
    # 读一个不存在的文件：错误回灌后模型（脚本）正常收尾，进程不崩
    call = ToolCall(id="t1", name="Read",
                    arguments={"file_path": str(tmp_path / "nope.txt")})
    provider = FakeProvider([_tool_resp([call]), _end_resp("已知晓错误")])
    loop = AgentLoop(provider, max_turns=5, permission_gate=BYPASS_GATE)

    assert loop.run("读文件") == "已知晓错误"
    assert loop.messages[2].is_error


def test_max_turns_guard_stops_loop():
    loop = AgentLoop(LoopForever(), max_turns=3, permission_gate=BYPASS_GATE)
    out = loop.run("无限循环测试")
    assert "上限" in out
    # 恰好执行 3 轮工具（每轮 1 个 tool_call）
    assert len([m for m in loop.messages if m.role == "tool"]) == 3


def test_usage_accumulated():
    provider = FakeProvider([_end_resp("直接回答")])
    loop = AgentLoop(provider, max_turns=5, permission_gate=BYPASS_GATE)
    loop.run("你好")
    assert loop.total_prompt_tokens == 10
    assert loop.total_completion_tokens == 5
    assert loop.last_usage == (10, 5, loop.last_usage[2])


def test_max_tokens_truncation_notice():
    provider = FakeProvider([LLMResponse(text="前半段", tool_calls=[],
                                         stop_reason=StopReason.MAX_TOKENS,
                                         prompt_tokens=10, completion_tokens=5)])
    loop = AgentLoop(provider, max_turns=5, permission_gate=BYPASS_GATE)
    out = loop.run("x")
    assert out.startswith("前半段") and "截断" in out and "max_tokens" in out


def test_max_tokens_no_text_still_notifies():
    provider = FakeProvider([LLMResponse(text=None, tool_calls=[],
                                         stop_reason=StopReason.MAX_TOKENS)])
    out = AgentLoop(provider, max_turns=5, permission_gate=BYPASS_GATE).run("x")
    assert "截断" in out


# ---------- P8：流式 provider ----------

class FakeStreamProvider:
    """有 chat_stream 能力：增量回调后弹脚本响应；chat 被调到即失败"""

    def __init__(self, script, deltas):
        self.script = list(script)
        self.deltas = list(deltas)

    def chat_stream(self, messages, tools=None, system="", on_text=None):
        for d in self.deltas:
            on_text(d)
        return self.script.pop(0)

    def chat(self, *a, **k):
        raise AssertionError("有流式能力时 loop 不应走非流式路径")


def test_streamed_text_printed_live_and_marked(capsys):
    provider = FakeStreamProvider([_end_resp("你好世界")], ["你好", "世界"])
    loop = AgentLoop(provider, max_turns=5, permission_gate=BYPASS_GATE)

    out = loop.run("打个招呼")
    assert out == "你好世界"
    assert loop.last_streamed is True          # main 据此不重印
    printed = capsys.readouterr().out
    assert "你好" in printed and "世界" in printed   # 增量实时进了 stdout

def test_non_stream_provider_marks_not_streamed(capsys):
    provider = FakeProvider([_end_resp("直接回答")])
    loop = AgentLoop(provider, max_turns=5, permission_gate=BYPASS_GATE)

    out = loop.run("问")
    assert out == "直接回答"
    assert loop.last_streamed is False
    assert "直接回答" not in capsys.readouterr().out   # loop 不打印，由 main 打

def test_streamed_max_tokens_notice_goes_to_stdout(capsys):
    provider = FakeStreamProvider(
        [LLMResponse(text="前半", tool_calls=[], stop_reason=StopReason.MAX_TOKENS,
                     prompt_tokens=1, completion_tokens=1)], ["前半"])
    loop = AgentLoop(provider, max_turns=5, permission_gate=BYPASS_GATE)

    out = loop.run("x")
    assert out == "前半"                        # 返回值不带提示（已流式打印）
    printed = capsys.readouterr().out
    assert "截断" in printed and loop.last_streamed is True


# ---------- P17 M1：allowed_tools / sink / budget_pool / agent_id ----------

def test_allowed_tools_filters_defs_and_rejects_outside(ws, tmp_path):
    seen = {}

    class SpyProvider(FakeProvider):
        def chat(self, messages, tools=None, system=""):
            seen["tools"] = [t.name for t in (tools or [])]
            return super().chat(messages, tools, system)

    target = str(tmp_path / "no.txt")
    call = ToolCall(id="t1", name="Write",
                    arguments={"file_path": target, "content": "x"})
    provider = SpyProvider([_tool_resp([call]), _end_resp()])
    loop = AgentLoop(provider, max_turns=5, permission_gate=BYPASS_GATE,
                     allowed_tools={"Read"})
    loop.run("受限工具集")
    assert seen["tools"] == ["Read"]                     # 告知模型的清单已过滤
    err = next(m for m in loop.messages if m.role == "tool")
    assert err.is_error                                  # 越界调用被拒并回灌
    assert "Write" in err.content or "白名单" in err.content
    assert not os.path.exists(target)                    # 未执行


def test_sink_captures_lines_instead_of_stdout(ws, tmp_path, capsys):
    class ListSink:
        def __init__(self):
            self.lines = []

        def write(self, line):
            self.lines.append(line)

        def event(self, line):
            self.lines.append(line)

    target = str(tmp_path / "s.txt")
    call = ToolCall(id="t1", name="Write",
                    arguments={"file_path": target, "content": "hi"})
    sink = ListSink()
    provider = FakeProvider([_tool_resp([call]), _end_resp()])
    AgentLoop(provider, max_turns=5, permission_gate=BYPASS_GATE,
              sink=sink).run("捕获输出")
    out = capsys.readouterr().out
    assert "● Write(" not in out                         # 不再直印
    assert any("● Write(" in ln for ln in sink.lines)    # 事件行入 transcript
    assert any("✓" in ln for ln in sink.lines)           # 结果行入 transcript
    assert sink.lines.index(next(ln for ln in sink.lines if "●" in ln)) < \
        sink.lines.index(next(ln for ln in sink.lines if "✓" in ln))   # 行序保持


def test_budget_pool_exhaustion_terminates_with_distinct_message(ws, tmp_path):
    from agent.budget import BudgetPool
    call = ToolCall(id="t1", name="Read", arguments={"file_path": "x"})
    resp1 = LLMResponse(text=None, tool_calls=[call], stop_reason=StopReason.TOOL_USE,
                        prompt_tokens=10, completion_tokens=2)
    resp2 = LLMResponse(text=None, tool_calls=[call], stop_reason=StopReason.TOOL_USE,
                        prompt_tokens=10, completion_tokens=2)
    provider = FakeProvider([resp1, resp2, _end_resp()])
    pool = BudgetPool(20)
    loop = AgentLoop(provider, max_turns=10, token_budget=999999,
                     permission_gate=BYPASS_GATE, budget_pool=pool)
    result = loop.run("烧池")
    assert result.startswith("[任务终止]") and "预算池" in result   # 区别于单任务预算文案
    assert pool.used == 12            # consume(12) 成功后仅剩 8，第二次 12>8 拒付（不扣减）即终止
    assert len(provider.script) == 1  # 第三次 LLM 调用未发生


def test_agent_id_recorded_in_journal_events():
    class FakeJournal:
        def __init__(self):
            self.events = []

        def log(self, event, **fields):
            self.events.append((event, fields))

    j = FakeJournal()
    AgentLoop(FakeProvider([_end_resp()]), permission_gate=BYPASS_GATE,
              journal=j, agent_id="sub-1").run("hi")
    start = next(f for e, f in j.events if e == "task_start")
    assert start["agent"] == "sub-1"
