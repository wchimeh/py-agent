# @File:     __init__.py
# @DateTime: 2026/09/16
"""Prompt 版本管理：{name}_v{N}.md 文件即版本，默认加载最新版。"""
import re
from importlib.resources import files


class PromptError(Exception):
    """prompt 名称不存在或指定版本不存在。"""


_NAME_RX = re.compile(r"^([a-z0-9_]+)_v(\d+)\.md$")


def _versions(name: str, _dir=None) -> dict[int, object]:
    """扫描目录返回 {版本号: 路径}；该名称无任何版本时抛 PromptError。"""
    base = _dir if _dir is not None else files("agent.prompts")
    out = {}
    for p in base.iterdir():
        m = _NAME_RX.match(p.name)
        if m and m.group(1) == name:
            out[int(m.group(2))] = p
    if not out:
        raise PromptError(f"prompt {name!r} 不存在：目录中无 {name}_vN.md 文件")
    return out


def latest_version(name: str, _dir=None) -> int:
    return max(_versions(name, _dir))


def load_prompt(name: str, version: int | None = None, _dir=None) -> str:
    """version=None 加载该名称最新版；指定版本不存在抛 PromptError（列出可用版本）。
    version 接受 int 或数字字符串（yaml 透传），<1 或非数字报清晰错误。
    _dir 仅供测试注入临时目录。"""
    if version is not None:
        try:
            version = int(version)
        except (TypeError, ValueError):
            raise PromptError(f"prompt {name} 版本号须为整数：{version!r}") from None
        if version < 1:
            raise PromptError(f"prompt {name} 版本号须 >= 1：{version}")
    vers = _versions(name, _dir)
    v = version if version is not None else max(vers)
    if v not in vers:
        avail = ", ".join(f"v{n}" for n in sorted(vers))
        raise PromptError(f"prompt {name} 版本 v{v} 不存在，可用：{avail}")
    return vers[v].read_text(encoding="utf-8")


def load_system_prompt(version: int | None = None) -> str:
    return load_prompt("system", version)
