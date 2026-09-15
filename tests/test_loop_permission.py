# -*- coding: utf-8 -*-
# @File:     test_loop_permission.py
# @Author:   mjh
# @DateTime: 2026/03/15
"""AgentLoop × PermissionGate 集成测试：拒绝回灌、bypass 直通、会话记忆跨任务。"""
from agent.providers.base import LLMResponse, StopReason, ToolCall
from agent.loop import AgentLoop
from agent.permissions import PermissionGate
from test_loop import FakeProvider


def _write_call(path):
    return ToolCall(id="t1", name="Write",
                    arguments={"file_path": path, "content": "x"})


def _script(calls, end_text="完成"):
    script = [LLMResponse(text=None, tool_calls=[c], stop_reason=StopReason.TOOL_USE)
              for c in calls]
    script.append(LLMResponse(text=end_text, tool_calls=[],
                              stop_reason=StopReason.END_TURN))
    return script


def test_default_deny_write_fed_back(tmp_path):
    # default 模式答 n：工具不执行（文件未落盘），拒绝原因作为错误回灌给模型
    target = str(tmp_path / "a.txt")
    gate = PermissionGate("default", ask_fn=lambda tc, danger: "n")
    provider = FakeProvider(_script([_write_call(target)]))
    loop = AgentLoop(provider, max_turns=5, permission_gate=gate)

    assert loop.run("写文件") == "完成"
    import os
    assert not os.path.exists(target)
    tr = loop.messages[2]
    assert tr.is_error and "拒绝" in tr.content


def test_bypass_writes_without_asking(ws, tmp_path):
    # bypass 模式全程不弹询问（若被问则答 n 让测试失败），文件直接落盘
    target = str(tmp_path / "a.txt")

    def fail_if_asked(tc, danger):
        return "n"

    gate = PermissionGate("bypass", ask_fn=fail_if_asked)
    provider = FakeProvider(_script([_write_call(target)]))
    loop = AgentLoop(provider, max_turns=5, permission_gate=gate)

    loop.run("写文件")
    assert open(target, encoding="utf-8").read() == "x"


def test_always_memory_across_tasks(ws, tmp_path):
    # 第一个任务答 a 后，第二个任务同键（Write）不再询问；两个文件都落盘
    f1, f2 = str(tmp_path / "1.txt"), str(tmp_path / "2.txt")

    asked = []

    def ask_always_a(tc, danger):
        asked.append(tc.name)
        return "a"

    gate = PermissionGate("default", ask_fn=ask_always_a)
    provider = FakeProvider(_script([_write_call(f1)]) + _script([_write_call(f2)]))
    loop = AgentLoop(provider, max_turns=10, permission_gate=gate)

    loop.run("任务1")
    loop.run("任务2")
    assert open(f1, encoding="utf-8").read() == "x"
    assert open(f2, encoding="utf-8").read() == "x"
    assert asked == ["Write"]                 # 只有第一次问了
    assert gate.always == {"Write"}


def test_workspace_boundary_holds_under_bypass(ws, tmp_path_factory):
    # 硬边界在工具层：bypass 权限放行后，工作区外写入仍被拒并作为错误回灌
    outside = tmp_path_factory.mktemp("outside")
    target = str(outside / "a.txt")
    gate = PermissionGate("bypass")
    provider = FakeProvider(_script([_write_call(target)]))
    loop = AgentLoop(provider, max_turns=5, permission_gate=gate)

    assert loop.run("写文件") == "完成"
    import os
    assert not os.path.exists(target)
    tr = loop.messages[2]
    assert tr.is_error and "越界" in tr.content
