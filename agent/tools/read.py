# @File:     read.py
# @Author:   mjh
# @DateTime: 2026/03/14/16:53
import os
from itertools import islice
from typing import ClassVar

from .base import Tool, ToolError
from .registry import register


@register
class ReadTool(Tool):
    name = "Read"
    description = "读取文本文件，输出带行号。默认前 2000 行；单行超 2000 字符会截断。"
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "文件绝对路径"},
            "offset": {"type": "integer", "description": "起始行号（1 起）"},
            "limit": {"type": "integer", "description": "读取行数，默认 2000"},
        },
        "required": ["file_path"],
    }

    def execute(self, file_path: str, offset: int = 1, limit: int = 2000) -> str:
        if not os.path.isfile(file_path):
            raise ToolError(f"文件不存在：{file_path}")
        # 懒读取：只取 offset/limit 窗口内的行，超大文件不再整读进内存
        with open(file_path, encoding="utf-8", errors="replace") as f:
            lines = [ln.rstrip("\n")[:2000]
                     for ln in islice(f, offset - 1, offset - 1 + limit)]
        if not lines:
            return "(空文件或 offset 超出行数)"
        return "\n".join(f"{n:>6}\t{line}" for n, line in enumerate(lines, offset))
