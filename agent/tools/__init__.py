# -*- coding: utf-8 -*-
# @File:     __init__.py
# @Author:   mjh
# @DateTime: 2026/03/14/16:58
from .registry import register, get_tool_defs, execute_tool
from .base import Tool, ToolError
from . import read, glob, grep, write, edit, bash   # 触发 @register


