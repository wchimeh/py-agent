# -*- coding: utf-8 -*-
# @File:     edit.py
# @Author:   mjh
# @DateTime: 2026/03/14/16:57
import os
from .base import Tool, ToolError
from .registry import register
from .workspace import WorkspaceError, resolve_writable


@register
class EditTool(Tool):
    name = "Edit"
    description = ("精确字符串替换。old_string 必须与文件内容完全一致且唯一；"
                   "不唯一时增加上下文，或 replace_all=true。"
                   "仅允许编辑工作区内文件，越界直接报错。")
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "old_string": {"type": "string", "description": "被替换文本（含足够上下文保证唯一）"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
        },
        "required": ["file_path", "old_string", "new_string"],
    }

    def execute(self, file_path: str, old_string: str, new_string: str,
                replace_all: bool = False) -> str:
        try:
            target = resolve_writable(file_path)
        except WorkspaceError as e:
            raise ToolError(str(e))
        if not os.path.isfile(target):
            raise ToolError(f"文件不存在：{target}")
        text = open(target, encoding="utf-8").read()
        n = text.count(old_string)
        if n == 0:
            raise ToolError("old_string 未找到，请先 Read 确认原文（注意空白与缩进）")
        if n > 1 and not replace_all:
            raise ToolError(f"old_string 出现 {n} 次，请增加上下文使其唯一，或设 replace_all=true")
        new_text = text.replace(old_string, new_string) if replace_all \
            else text.replace(old_string, new_string, 1)
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(new_text)
        return f"已替换（{n if replace_all else 1} 处）"
