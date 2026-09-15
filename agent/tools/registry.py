# -*- coding: utf-8 -*-
# @File:     registry.py
# @Author:   mjh
# @DateTime: 2026/03/14/16:39
from ..providers.base import ToolCall, ToolDef
from .base import Tool, ToolError

_TOOLS: dict[str, Tool] = {}


def register(cls):
    tool = cls()
    _TOOLS[tool.name] = tool
    return cls


def get_tool_defs() -> list[ToolDef]:

    return [ToolDef(name=t.name, description=t.description, parameters=t.parameters) for t in _TOOLS.values()]


def execute_tool(tc: ToolCall) -> tuple[str, bool]:
    tool = _TOOLS.get(tc.name)

    if tool is None:
        return f"未知工具：{tc.name}，可用工具：{', '.join(_TOOLS)}", True
    try:
        return tool.execute(**tc.arguments), False
    except ToolError as e:
        return str(e), True
    except Exception as e:
        return f"{type(e).__name__}: {e}", True



