# @File:     test_tools.py
# @Author:   mjh
# @DateTime: 2026/03/14
"""工具层单测：只测纯本地逻辑，不依赖网络与真实 API。"""
import os
import subprocess
import sys
from typing import ClassVar

import pytest

from agent.providers.base import ToolCall
from agent.tools.base import truncate
from agent.tools.bash import BashTool
from agent.tools.edit import EditTool
from agent.tools.glob import GlobTool
from agent.tools.grep import GrepTool
from agent.tools.read import ReadTool
from agent.tools.registry import _TOOLS, execute_tool
from agent.tools.write import WriteTool

# ---------- base.truncate ----------

def test_truncate_short_text_untouched():
    assert truncate("hello") == "hello"

def test_truncate_long_text_keeps_head_and_tail():
    text = "A" * 100 + "MIDDLE" + "B" * 100
    out = truncate(text, head=50, tail=50)
    assert out.startswith("A" * 50)
    assert out.endswith("B" * 50)
    assert "已截断" in out


# ---------- Read ----------

def test_read_with_line_numbers(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello\nworld\n", encoding="utf-8")
    out = ReadTool().execute(file_path=str(f))
    assert "     1\thello" in out and "     2\tworld" in out

def test_read_offset_and_limit(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 6)), encoding="utf-8")
    out = ReadTool().execute(file_path=str(f), offset=2, limit=2)
    assert "line2" in out and "line3" in out
    assert "line1" not in out and "line4" not in out

def test_read_empty_file(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_text("", encoding="utf-8")
    assert ReadTool().execute(file_path=str(f)) == "(空文件或 offset 超出行数)"

def test_read_truncates_long_lines(tmp_path):
    # minified 文件单行 10 万字符：输出必须截到 2000，行号前缀另算
    f = tmp_path / "long.txt"
    f.write_text("A" * 100000 + "\nshort\n", encoding="utf-8")
    out = ReadTool().execute(file_path=str(f))
    lines = out.split("\n")
    assert max(len(ln) for ln in lines) <= 6 + 1 + 2000   # 行号6位 + \t + 截断
    assert "short" in out

def test_read_offset_out_of_range(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("one\n", encoding="utf-8")
    assert ReadTool().execute(file_path=str(f), offset=99) == "(空文件或 offset 超出行数)"

def test_read_missing_file_raises_tool_error(tmp_path):
    with pytest.raises(Exception, match="文件不存在"):
        ReadTool().execute(file_path=str(tmp_path / "no_such_file.xyz"))


# ---------- Glob ----------

def test_glob_matches(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "b.py").write_text("x", encoding="utf-8")
    out = GlobTool().execute(pattern="*.txt", path=str(tmp_path))
    assert "a.txt" in out and "b.py" not in out

def test_glob_no_match(tmp_path):
    assert GlobTool().execute(pattern="*.zzz", path=str(tmp_path)) == "未匹配到文件"

def test_glob_skips_dependency_dirs(tmp_path):
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "dep.py").write_text("x", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "c.py").write_text("x", encoding="utf-8")
    (tmp_path / "ok.py").write_text("x", encoding="utf-8")
    out = GlobTool().execute(pattern="**/*.py", path=str(tmp_path))
    assert "ok.py" in out and "dep.py" not in out and "c.py" not in out

def test_glob_double_star_includes_root_and_nested(tmp_path):
    (tmp_path / "top.py").write_text("x", encoding="utf-8")
    deep = tmp_path / "src" / "deep"
    deep.mkdir(parents=True)
    (deep / "in.py").write_text("x", encoding="utf-8")
    out = GlobTool().execute(pattern="**/*.py", path=str(tmp_path))
    assert "top.py" in out and "in.py" in out

def test_glob_flat_pattern_stays_top_level(tmp_path):
    (tmp_path / "top.py").write_text("x", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "in.py").write_text("x", encoding="utf-8")
    out = GlobTool().execute(pattern="*.py", path=str(tmp_path))
    assert "top.py" in out and "in.py" not in out

def test_glob_subdir_pattern(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "docs" / "sub").mkdir()
    (tmp_path / "docs" / "sub" / "b.md").write_text("x", encoding="utf-8")
    out = GlobTool().execute(pattern="docs/*.md", path=str(tmp_path))
    assert "a.md" in out and "b.md" not in out

def test_glob_mtime_desc_order(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("x", encoding="utf-8")
    b = tmp_path / "b.txt"
    b.write_text("x", encoding="utf-8")
    os.utime(a, (1000000, 1000000))   # a 更旧，b 应排前
    out = GlobTool().execute(pattern="*.txt", path=str(tmp_path))
    assert out.index("b.txt") < out.index("a.txt")

def test_glob_caps_at_200(tmp_path):
    for i in range(205):
        (tmp_path / f"f{i:03}.txt").write_text("x", encoding="utf-8")
    out = GlobTool().execute(pattern="*.txt", path=str(tmp_path))
    assert len(out.split("\n")) == 200


# ---------- Grep ----------

def test_grep_finds_match_with_line_number(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    (d / "code.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    out = GrepTool().execute(pattern=r"b\s*=", path=str(d))
    assert "code.py:2:b = 2" in out.replace("\\", "/") or "b = 2" in out

def test_grep_no_match(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    assert GrepTool().execute(pattern="zzz", path=str(tmp_path)) == "无匹配"

def test_grep_invalid_regex(tmp_path):
    with pytest.raises(Exception, match="正则表达式无效"):
        GrepTool().execute(pattern="([unclosed", path=str(tmp_path))


# ---------- Write ----------

def test_write_creates_file_and_parent_dirs(ws, tmp_path):
    f = tmp_path / "sub" / "dir" / "a.txt"
    out = WriteTool().execute(file_path=str(f), content="hello\n")
    assert f.read_text(encoding="utf-8") == "hello\n"
    assert "已写入" in out

def test_write_overwrites_existing(ws, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("old", encoding="utf-8")
    WriteTool().execute(file_path=str(f), content="new")
    assert f.read_text(encoding="utf-8") == "new"

def test_write_outside_workspace_rejected(ws, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    with pytest.raises(Exception, match="越界"):
        WriteTool().execute(file_path=str(outside / "a.txt"), content="x")

def test_write_dotdot_escape_rejected(ws):
    with pytest.raises(Exception, match="越界"):
        WriteTool().execute(file_path=str(ws / ".." / "evil.txt"), content="x")

def test_write_relative_resolved_against_workspace(ws):
    out = WriteTool().execute(file_path="rel/a.txt", content="hi")
    assert (ws / "rel" / "a.txt").read_text(encoding="utf-8") == "hi"
    assert "已写入" in out


# ---------- Edit ----------

def test_edit_unique_replace(ws, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello\nworld\n", encoding="utf-8")
    out = EditTool().execute(file_path=str(f), old_string="world",
                             new_string="python")
    assert f.read_text(encoding="utf-8") == "hello\npython\n"
    assert "已替换" in out

def test_edit_not_found(ws, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello\n", encoding="utf-8")
    with pytest.raises(Exception, match="未找到"):
        EditTool().execute(file_path=str(f), old_string="zzz", new_string="x")

def test_edit_ambiguous_without_replace_all(ws, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("foo\nfoo\n", encoding="utf-8")
    with pytest.raises(Exception, match="2 次"):
        EditTool().execute(file_path=str(f), old_string="foo", new_string="bar")

def test_edit_replace_all(ws, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("foo\nfoo\n", encoding="utf-8")
    EditTool().execute(file_path=str(f), old_string="foo", new_string="bar",
                       replace_all=True)
    assert f.read_text(encoding="utf-8") == "bar\nbar\n"

def test_edit_outside_workspace_rejected(ws, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    f = outside / "a.txt"
    f.write_text("hello", encoding="utf-8")
    with pytest.raises(Exception, match="越界"):
        EditTool().execute(file_path=str(f), old_string="hello", new_string="x")


# ---------- Bash ----------

def test_bash_echo():
    out = BashTool().execute(command="echo hi")
    assert "hi" in out

def test_bash_nonzero_exit_is_error_with_output():
    with pytest.raises(Exception, match=r"退出码 3"):
        BashTool().execute(command="exit 3")

def test_bash_timeout():
    # sys.executable 而非裸 `python`：Ubuntu 24.04 等裸系统只有 python3（CI 修复 2026-09-16）
    with pytest.raises(Exception, match="超时"):
        BashTool().execute(
            command=f'"{sys.executable}" -c "import time; time.sleep(5)"',
            timeout=1)


# ---------- P16 M1：Bash 委托沙箱执行器 ----------

def test_bash_delegates_to_run_command(monkeypatch):
    from agent import sandbox
    calls = {}

    def fake_run(command, timeout):
        calls["args"] = (command, timeout)
        return (0, "from-executor\n", "")

    monkeypatch.setattr(sandbox, "run_command", fake_run)
    assert BashTool().execute(command="echo x", timeout=5) == "from-executor"
    assert calls["args"] == ("echo x", 5)


def test_bash_executor_timeout_becomes_tool_error(monkeypatch):
    from agent import sandbox

    def fake_run(command, timeout):
        raise subprocess.TimeoutExpired(cmd=command, timeout=timeout)

    monkeypatch.setattr(sandbox, "run_command", fake_run)
    with pytest.raises(Exception, match="超时"):
        BashTool().execute(command="sleep 9", timeout=9)


def test_bash_executor_nonzero_becomes_tool_error(monkeypatch):
    from agent import sandbox
    monkeypatch.setattr(sandbox, "run_command", lambda c, t: (3, "out-line", "err-line"))
    with pytest.raises(Exception, match=r"退出码 3"):
        BashTool().execute(command="bad")


def test_bash_description_refreshes_for_docker(monkeypatch):
    from agent import sandbox
    from agent.tools.bash import DEFAULT_DESCRIPTION, BashTool, refresh_description

    try:
        monkeypatch.setattr(sandbox, "_EXECUTOR",
                            sandbox.DockerExecutor("c", "img", "/ws"))
        refresh_description()
        assert "容器" in BashTool.description and "/workspace" in BashTool.description
        monkeypatch.setattr(sandbox, "_EXECUTOR", sandbox.LocalExecutor())
        refresh_description()
        assert BashTool.description == DEFAULT_DESCRIPTION
    finally:
        from agent.tools.bash import refresh_description as rd
        monkeypatch.setattr(sandbox, "_EXECUTOR", sandbox.LocalExecutor())
        rd()


# ---------- registry / execute_tool ----------

def test_all_six_tools_registered():
    assert set(_TOOLS) == {"Read", "Glob", "Grep", "Write", "Edit", "Bash"}

def _run(name, **kw):
    return execute_tool(ToolCall(id="t", name=name, arguments=kw))

def test_execute_tool_success_tuple():
    content, is_error = _run("Bash", command="echo ok")
    assert is_error is False and "ok" in content

def test_execute_tool_unknown_tool():
    content, is_error = _run("NotExist", foo=1)
    assert is_error is True and "未知工具" in content

def test_execute_tool_never_raises(tmp_path):
    # 工具内部抛任意异常都转为 (错误信息, True)，不击穿循环
    _, is_error = _run("Read", file_path=str(tmp_path))  # 目录不是文件
    assert is_error is True


# ---------- registry 参数校验（P9：LLM 输出边界） ----------

def test_validate_missing_required_param():
    content, is_error = _run("Bash")  # 缺 command
    assert is_error is True
    assert "缺少必填参数" in content and "command" in content

def test_validate_unknown_param():
    content, is_error = _run("Bash", command="echo ok", bogus=1)
    assert is_error is True
    assert "未知参数" in content and "bogus" in content

def test_validate_uncoercible_type():
    content, is_error = _run("Bash", command="echo ok", timeout="abc")
    assert is_error is True
    assert "timeout" in content

def test_validate_type_error_in_chinese():
    # P12 冒烟发现：int_parsing 等类型错误回灌英文原文，补全 _ERR_ZH 中文映射
    content, _ = _run("Bash", command="echo ok", timeout="soon")
    assert "应为整数" in content

def test_validate_lax_coercion():
    # lax 语义固化："5"→5 自动转换不报错（用户确认的宽松行为）
    content, is_error = _run("Bash", command="echo ok", timeout="5")
    assert is_error is False and "ok" in content

def test_validate_enum_violation():
    from agent.tools import registry
    from agent.tools.base import Tool

    @registry.register
    class EnumFake(Tool):
        name = "EnumFake"
        description = "测试用"
        parameters: ClassVar[dict] = {"type": "object",
                      "properties": {"mode": {"type": "string", "enum": ["a", "b"]}},
                      "required": ["mode"]}

        def execute(self, mode):
            return f"ok:{mode}"

    try:
        content, is_error = _run("EnumFake", mode="c")
        assert is_error is True and "mode" in content
        content, is_error = _run("EnumFake", mode="a")
        assert is_error is False and content == "ok:a"
    finally:
        registry._TOOLS.pop("EnumFake", None)
        getattr(registry, "_MODELS", {}).pop("EnumFake", None)

def test_validate_omitted_optional_uses_execute_default():
    # exclude_unset 语义：省略 timeout 时不传 None，execute 默认值 120 生效
    content, is_error = _run("Bash", command="echo ok")
    assert is_error is False and "ok" in content

def test_execute_tool_malformed_json_sentinel():
    # provider 解析失败塞入的哨兵 → 明确的解析错误回灌，不进 schema 校验也不执行
    from agent.providers.base import MALFORMED_ARGS_KEY
    content, is_error = _run("Bash", **{MALFORMED_ARGS_KEY: '{"command": '})
    assert is_error is True
    assert "不是合法 JSON" in content and "command" in content

def test_sentinel_requires_sole_key():
    # 哨兵与其他参数混合时不是解析错误，走 schema 校验（未知参数）
    from agent.providers.base import MALFORMED_ARGS_KEY
    content, is_error = _run("Bash", **{MALFORMED_ARGS_KEY: "not json",
                                       "command": "echo ok"})
    assert is_error is True
    assert "未知参数" in content

def test_validation_error_schema_truncated():
    from agent.tools import registry
    from agent.tools.base import Tool

    @registry.register
    class BigSchema(Tool):
        name = "BigSchema"
        description = "测试用"
        parameters: ClassVar[dict] = {"type": "object",
                      "properties": {"x": {"type": "string",
                                           "description": "长" * 2000}},
                      "required": ["x"]}

        def execute(self, x):
            return "ok"

    try:
        content, is_error = _run("BigSchema")   # 缺必填，错误回灌附 schema
        assert is_error is True
        assert len(content) <= 1200             # schema 超长必须截断
    finally:
        registry._TOOLS.pop("BigSchema", None)
        registry._MODELS.pop("BigSchema", None)
