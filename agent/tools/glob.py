# -*- coding: utf-8 -*-
# @File:     glob.py
# @Author:   mjh
# @DateTime: 2026/03/15
import fnmatch
import os
from .base import SKIP_DIRS, Tool
from .registry import register


def _match_parts(name_parts: list[str], pat_parts: list[str]) -> bool:
    """逐段匹配，** 段匹配零或多层目录，* 不跨分隔符。"""
    if not pat_parts:
        return not name_parts
    head = pat_parts[0]
    if head == "**":
        return _match_parts(name_parts, pat_parts[1:]) or \
            (bool(name_parts) and _match_parts(name_parts[1:], pat_parts))
    if not name_parts:
        return False
    return fnmatch.fnmatch(name_parts[0], head) and \
        _match_parts(name_parts[1:], pat_parts[1:])


@register
class GlobTool(Tool):
    name = "Glob"
    description = ("按模式匹配文件路径（如 **/*.py），按修改时间倒序，上限 200 条；"
                   "自动跳过 .git/.venv/__pycache__/node_modules 等目录。")
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob 模式，** 匹配任意层目录"},
            "path": {"type": "string", "description": "搜索目录，默认当前目录"},
        },
        "required": ["pattern"],
    }

    def execute(self, pattern: str, path: str = ".") -> str:
        pattern = pattern.replace("\\", "/").lstrip("/")
        pat_parts = pattern.split("/")
        root = os.path.abspath(path)
        hits: list[tuple[float, str]] = []
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            depth = 0 if rel_dir == "." else len(rel_dir.split(os.sep))
            # 剪枝：跳过依赖/缓存目录；无 ** 时更深的文件不可能命中
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS
                           and ("**" in pat_parts or depth + 1 < len(pat_parts))]
            for name in filenames:
                rel = os.path.relpath(os.path.join(dirpath, name), root)
                if _match_parts(rel.split(os.sep), pat_parts):
                    full = os.path.join(dirpath, name)
                    try:
                        hits.append((os.path.getmtime(full), full))
                    except OSError:
                        continue
        if not hits:
            return "未匹配到文件"
        hits.sort(key=lambda h: h[0], reverse=True)
        return "\n".join(p for _, p in hits[:200])
