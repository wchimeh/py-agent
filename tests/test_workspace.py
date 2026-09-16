# @File:     test_workspace.py
# @Author:   mjh
# @DateTime: 2026/03/15
"""workspace.resolve_writable 越界判定矩阵：合法路径、逃逸、异盘、大小写、符号链接。"""
import os
import sys

import pytest

from agent.tools import workspace
from agent.tools.workspace import WorkspaceError, get_root, resolve_writable, set_root


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACE_ROOT", str(tmp_path))
    return tmp_path


def test_inside_absolute_ok(root):
    target = root / "sub" / "a.txt"
    assert resolve_writable(str(target)) == os.path.realpath(str(target))


def test_relative_resolved_against_root_not_cwd(root, tmp_path_factory, monkeypatch):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    monkeypatch.chdir(elsewhere)
    assert resolve_writable("a.txt") == os.path.realpath(str(root / "a.txt"))


def test_dotdot_escape_rejected(root):
    with pytest.raises(WorkspaceError, match="工作区"):
        resolve_writable(str(root / ".." / "evil.txt"))


def test_other_drive_rejected(root):
    if sys.platform == "win32":
        drive = os.path.splitdrive(str(root))[0]
        other = "Q:" if drive.upper() != "Q:" else "R:"
        evil = f"{other}\\evil.txt"
    else:
        evil = "/evil.txt"
    with pytest.raises(WorkspaceError):
        resolve_writable(evil)


def test_sibling_prefix_trap_rejected(root):
    # 字符串前缀相近的兄弟目录不能因 startswith 式判定混进来
    sibling = root.parent / (root.name + "_tail")
    with pytest.raises(WorkspaceError):
        resolve_writable(str(sibling / "a.txt"))


@pytest.mark.skipif(sys.platform != "win32", reason="路径大小写不敏感是 Windows 行为")
def test_case_variant_inside_ok(root):
    variant = str(root).upper() + "\\a.txt"
    assert os.path.normcase(resolve_writable(variant)) == \
        os.path.normcase(os.path.realpath(str(root / "a.txt")))


def test_symlink_outside_rejected(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("x", encoding="utf-8")
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境无符号链接创建权限")
    with pytest.raises(WorkspaceError):
        resolve_writable(str(link / "secret.txt"))


def test_set_root_normalizes(tmp_path):
    set_root(str(tmp_path))
    try:
        assert get_root() == os.path.realpath(str(tmp_path))
        assert resolve_writable(str(tmp_path / "ok.txt")).startswith(get_root())
    finally:
        set_root(os.getcwd())   # 还原，避免污染其他测试


# ---------- 任务 1-10：docker 模式 /workspace 前缀自动映射 ----------

@pytest.fixture
def docker_mode(root, monkeypatch):
    """激活 DockerExecutor（不触碰真实 docker，仅判型用）+ 工作区根指向 tmp_path。"""
    from agent import sandbox
    monkeypatch.setattr(sandbox, "_EXECUTOR",
                        sandbox.DockerExecutor("c", "img", str(root)))
    return root


def test_map_path_passthrough_in_local_mode(root):
    from agent.tools.workspace import map_path
    assert map_path("/workspace/a.py") == "/workspace/a.py"   # none 模式原样返回


def test_map_path_maps_prefix_in_docker_mode(docker_mode):
    from agent.tools.workspace import map_path
    root = str(docker_mode)
    assert map_path("/workspace/a.py") == os.path.join(root, "a.py")
    assert map_path("/workspace/sub/deep/b.py") == os.path.join(root, "sub", "deep", "b.py")


def test_map_path_bare_workspace_is_root(docker_mode):
    from agent.tools.workspace import map_path
    assert map_path("/workspace") == str(docker_mode)


def test_map_path_host_path_unchanged_in_docker_mode(docker_mode):
    from agent.tools.workspace import map_path
    host = os.path.join(str(docker_mode), "x.txt")
    assert map_path(host) == host                     # 宿主绝对路径不动
    assert map_path("rel.txt") == "rel.txt"           # 相对路径不动


def test_resolve_writable_accepts_container_path(docker_mode):
    target = os.path.realpath(os.path.join(str(docker_mode), "bubble_sort.py"))
    assert resolve_writable("/workspace/bubble_sort.py") == target


def test_resolve_writable_container_path_still_bounded(docker_mode):
    with pytest.raises(WorkspaceError):               # 映射后仍过越界检查
        resolve_writable("/workspace/../evil.txt")


def test_read_tool_reads_via_container_path(docker_mode):
    from agent.tools.read import ReadTool
    f = docker_mode / "hello.txt"
    f.write_text("第一行\n第二行", encoding="utf-8")
    out = ReadTool().execute(file_path="/workspace/hello.txt")
    assert "第一行" in out and "第二行" in out


def test_grep_glob_accept_container_path(docker_mode):
    from agent.tools.glob import GlobTool
    from agent.tools.grep import GrepTool
    (docker_mode / "g.txt").write_text("needle-here", encoding="utf-8")
    assert "needle-here" in GrepTool().execute(pattern="needle", path="/workspace")
    assert "g.txt" in GlobTool().execute(pattern="*.txt", path="/workspace")
