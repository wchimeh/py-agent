# @File:     grep.py
# @Author:   mjh
# @DateTime: 2026/03/14/16:56
import re
from pathlib import Path
from typing import ClassVar

from .base import SKIP_DIRS, Tool, ToolError, truncate
from .registry import register


@register
class GrepTool(Tool):
    name = "Grep"
    description = "在文件内容中正则搜索，输出 path:line:content。"
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式"},
            "path": {"type": "string", "description": "搜索目录或单文件，默认当前目录"},
            "glob": {"type": "string", "description": "文件名过滤，如 *.py"},
        },
        "required": ["pattern"],
    }

    def execute(self, pattern: str, path: str = ".",
                glob: str | None = None, max_results: int = 200) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"正则表达式无效：{e}") from e
        root = Path(path)
        if root.is_file():
            files = [root]
        else:
            files = [p for p in root.rglob(glob or "*")
                     if p.is_file() and not (set(p.parts) & SKIP_DIRS)]
        hits: list[str] = []
        for f in files:
            text = f.read_text(encoding="utf-8", errors="ignore")
            if "\x00" in text[:1024]:  # 二进制跳过
                continue
            for no, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f}:{no}:{line.strip()}")
                    if len(hits) >= max_results:
                        return truncate("\n".join(hits), 20000, 0) + "\n(已达结果上限)"
        return "\n".join(hits) if hits else "无匹配"
