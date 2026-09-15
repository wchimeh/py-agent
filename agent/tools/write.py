# -*- coding: utf-8 -*-
# @File:     write.py
# @Author:   mjh
# @DateTime: 2026/03/14/16:56
from pathlib import Path
from .base import Tool, ToolError
from .registry import register
from .workspace import WorkspaceError, resolve_writable


@register
class WriteTool(Tool):
    name = "Write"
    description = ("写入整个文件（覆盖或新建），父目录不存在时自动创建。"
                   "仅允许写工作区内路径，越界直接报错。")
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "目标文件绝对路径"},
            "content": {"type": "string", "description": "完整文件内容"},
        },
        "required": ["file_path", "content"],
    }

    def execute(self, file_path: str, content: str) -> str:
        try:
            target = resolve_writable(file_path)
        except WorkspaceError as e:
            raise ToolError(str(e))
        p = Path(target)
        p.parent.mkdir(parents=True, exist_ok=True)
        # newline="" 防止 Windows 把 \n 自动转成 \r\n
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return f"已写入 {p}（{len(content.splitlines())} 行）"
