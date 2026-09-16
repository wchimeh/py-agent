# @File:     test_permissions.py
# @Author:   mjh
# @DateTime: 2026/03/15
"""PermissionGate 单测：注入假 ask_fn，全程无人工交互。"""
import pytest

from agent.permissions import PermissionGate, match_danger
from agent.providers.base import ToolCall


def tc(name, **kw):
    return ToolCall(id="t", name=name, arguments=kw)


def make(mode, answers=None):
    asked = []

    def fake_ask(t, danger):
        asked.append((t.name, danger))
        return answers.pop(0) if answers else "y"

    return PermissionGate(mode, ask_fn=fake_ask), asked


# ---------- 模式矩阵 ----------

def test_readonly_never_asks_in_any_mode():
    for mode in ("default", "acceptEdits", "bypass"):
        gate, asked = make(mode)
        assert gate.authorize(tc("Read", file_path="x")) == (True, "")
        assert gate.authorize(tc("Glob", pattern="*")) == (True, "")
        assert gate.authorize(tc("Grep", pattern="x")) == (True, "")
        assert asked == []

def test_write_asks_only_in_default():
    gate, asked = make("default")
    assert gate.authorize(tc("Write", file_path="a", content="x"))[0] is True
    assert len(asked) == 1                        # default 要问
    gate, asked = make("acceptEdits")
    assert gate.authorize(tc("Write", file_path="a", content="x")) == (True, "")
    assert gate.authorize(tc("Edit", file_path="a", old_string="x",
                             new_string="y")) == (True, "")
    assert asked == []                            # acceptEdits 文件编辑不问
    gate, asked = make("bypass")
    assert gate.authorize(tc("Write", file_path="a", content="x")) == (True, "")
    assert asked == []

def test_bash_asks_in_default_and_accept_edits_but_not_bypass():
    for mode in ("default", "acceptEdits"):
        gate, asked = make(mode)
        assert gate.authorize(tc("Bash", command="echo hi"))[0] is True
        assert len(asked) == 1
    gate, asked = make("bypass")
    assert gate.authorize(tc("Bash", command="echo hi")) == (True, "")
    assert asked == []

def test_unknown_tool_asks_conservatively():
    gate, asked = make("default")
    ok, _ = gate.authorize(tc("SomeNewTool", x=1))
    assert ok is True and len(asked) == 1         # 未知工具不直通，按需询问


# ---------- 危险命令识别 ----------

def test_danger_patterns():
    assert match_danger("rm -rf /tmp/x") is not None
    assert match_danger("RM -RF dir") is not None                 # 大小写不敏感
    assert match_danger("git push --force origin main") is not None
    assert match_danger("git push -f origin") is not None
    assert match_danger("del /s /q build") is not None
    assert match_danger("cd tmp && rd /s build") is not None      # 复合命令中段命中
    assert match_danger("echo hi") is None
    assert match_danger("python -c print(1)") is None
    assert match_danger("git status") is None

def test_danger_label_passed_to_ask():
    gate, asked = make("default")
    gate.authorize(tc("Bash", command="rm -rf /tmp/x"))
    assert asked[0][1] is not None               # ⚠ 原因传给了询问函数

def test_danger_beats_session_allow():
    # a 允许 Bash:cd 后，cd 借道夹带危险操作必须重新询问
    gate, asked = make("default", answers=["a", "n"])
    assert gate.authorize(tc("Bash", command="cd tmp"))[0] is True
    assert len(asked) == 1
    ok, _ = gate.authorize(tc("Bash", command="cd tmp && del /s /q x"))
    assert ok is False and len(asked) == 2


# ---------- 会话记忆 ----------

def test_key_is_first_token_for_bash():
    gate, _ = make("bypass")
    assert gate._key(tc("Bash", command="Python -c x")) == "Bash:python"
    assert gate._key(tc("Bash", command="git push x")) == "Bash:git"
    assert gate._key(tc("Write", file_path="a")) == "Write"

def test_always_remembered_per_key():
    gate, asked = make("default", answers=["a"])
    gate.authorize(tc("Bash", command="python a.py"))
    gate.authorize(tc("Bash", command="python b.py"))     # 同键不再问
    assert len(asked) == 1
    gate.authorize(tc("Bash", command="git status"))      # 不同键要问
    assert len(asked) == 2

def test_never_short_circuits_without_asking():
    gate, asked = make("default", answers=["e"])
    ok1, note1 = gate.authorize(tc("Bash", command="git push x"))
    ok2, note2 = gate.authorize(tc("Bash", command="git push y"))
    assert ok1 is False and ok2 is False
    assert len(asked) == 1                               # 第二次直接拒绝不再问
    assert "用户拒绝" in note1                           # 第一次：当场拒绝
    assert "会话内拒绝" in note2                         # 第二次：短路拒绝

def test_n_is_single_shot():
    gate, asked = make("default", answers=["n", "y"])
    assert gate.authorize(tc("Write", file_path="a", content="x"))[0] is False
    assert gate.authorize(tc("Write", file_path="a", content="x"))[0] is True
    assert len(asked) == 2                               # n 不留记忆，下次还问

def test_reject_note_mentions_tool_and_key():
    gate, _ = make("default", answers=["n"])
    ok, note = gate.authorize(tc("Write", file_path="a", content="x"))
    assert ok is False
    assert "Write" in note and "拒绝" in note


# ---------- 模式切换与非法值 ----------

def test_set_mode_switches_behavior():
    gate, asked = make("default", answers=["y"])
    gate.authorize(tc("Write", file_path="a", content="x"))
    assert len(asked) == 1
    gate.set_mode("bypass")
    assert gate.authorize(tc("Write", file_path="a", content="x")) == (True, "")
    assert len(asked) == 1                               # 切换后不再问

def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        PermissionGate("bad-mode")
    gate, _ = make("default")
    with pytest.raises(ValueError):
        gate.set_mode("yolo")


# ---------- P16 M1：沙箱 trusted 免审批（docker 内非危险 Bash 直通） ----------

def _docker_on(monkeypatch):
    from agent import sandbox
    monkeypatch.setattr(sandbox, "_EXECUTOR",
                        sandbox.DockerExecutor("agent-sandbox-test", "img", "/ws"))


def _trusted_gate(answers=None):
    asked = []

    def fake_ask(t, danger):
        asked.append((t.name, danger))
        return answers.pop(0) if answers else "y"

    return PermissionGate("default", ask_fn=fake_ask, sandbox_trusted=True), asked


def test_trusted_docker_allows_bash_without_asking(monkeypatch):
    _docker_on(monkeypatch)
    gate, asked = _trusted_gate()
    assert gate.authorize(tc("Bash", command="pip install requests")) == (True, "")
    assert asked == []                               # 沙箱内非危险命令免审批


def test_trusted_docker_danger_still_asks(monkeypatch):
    # 工作区是 rw 挂载：rm -rf /workspace 真能删宿主文件，危险命令不豁免
    _docker_on(monkeypatch)
    gate, asked = _trusted_gate(answers=["y"])
    assert gate.authorize(tc("Bash", command="rm -rf /workspace/x"))[0] is True
    assert len(asked) == 1 and asked[0][1] is not None


def test_trusted_local_executor_still_asks(monkeypatch):
    from agent import sandbox
    monkeypatch.setattr(sandbox, "_EXECUTOR", sandbox.LocalExecutor())
    gate, asked = _trusted_gate()
    assert gate.authorize(tc("Bash", command="echo hi"))[0] is True
    assert len(asked) == 1                           # trusted 只在 docker 模式生效


def test_untrusted_docker_still_asks(monkeypatch):
    _docker_on(monkeypatch)
    asked = []

    def fake_ask(t, danger):
        asked.append(t.name)
        return "y"

    gate = PermissionGate("default", ask_fn=fake_ask, sandbox_trusted=False)
    gate.authorize(tc("Bash", command="echo hi"))
    assert len(asked) == 1


def test_trusted_does_not_cover_write(monkeypatch):
    _docker_on(monkeypatch)
    gate, asked = _trusted_gate()
    gate.authorize(tc("Write", file_path="a", content="x"))
    assert len(asked) == 1                           # trusted 只罩 Bash，文件工具照常询问


def test_trusted_never_set_still_wins(monkeypatch):
    _docker_on(monkeypatch)
    gate, asked = _trusted_gate(answers=["e"])
    assert gate.authorize(tc("Bash", command="rm -rf /tmp/x"))[0] is False
    ok, _ = gate.authorize(tc("Bash", command="rm -rf /tmp/y"))
    assert ok is False and len(asked) == 1          # 危险命令 e 拒后短路，trusted 不复活它
