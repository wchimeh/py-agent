# @File:     workspace.py
# @Author:   mjh
# @DateTime: 2026/03/15
"""工作区路径边界：Write/Edit 只允许写工作区内（工具层硬边界，与权限模式无关）。"""
import os

from .. import sandbox


class WorkspaceError(Exception):
    """路径越界或不可判定（异盘等）。"""


WORKSPACE_ROOT: str = os.path.abspath(os.getcwd())


def set_root(path: str) -> None:
    global WORKSPACE_ROOT
    WORKSPACE_ROOT = os.path.realpath(os.path.abspath(path))


def get_root() -> str:
    return WORKSPACE_ROOT


def map_path(path: str) -> str:
    """docker 沙箱模式下容器路径 /workspace[...] 映射到宿主工作区根；其余原样返回。"""
    if not isinstance(sandbox.get_executor(), sandbox.DockerExecutor):
        return path
    if path == sandbox.CONTAINER_WORKSPACE:
        return WORKSPACE_ROOT
    prefix = sandbox.CONTAINER_WORKSPACE + "/"
    if path.startswith(prefix):
        return os.path.join(WORKSPACE_ROOT, *path[len(prefix):].split("/"))
    return path


def resolve_writable(path: str) -> str:
    """校验并返回规范化绝对路径；相对路径按工作区根解析（不随进程 cwd 漂移）。"""
    path = map_path(path)
    root = os.path.realpath(WORKSPACE_ROOT)
    p = path if os.path.isabs(path) else os.path.join(WORKSPACE_ROOT, path)
    target = os.path.realpath(os.path.abspath(p))
    n_root, n_target = os.path.normcase(root), os.path.normcase(target)
    try:
        inside = os.path.commonpath([n_root, n_target]) == n_root
    except ValueError:  # 不同盘符（如 G: 与 C:）无法比较
        inside = False
    if not inside:
        raise WorkspaceError(
            f"路径越界：{path}（解析为 {target}）不在工作区 {root} 内，"
            f"Write/Edit 仅允许写工作区内文件")
    return target
