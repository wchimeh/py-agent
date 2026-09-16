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
