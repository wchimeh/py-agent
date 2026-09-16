# @File:     base.py
# @Author:   mjh
# @DateTime: 2026/03/14/16:35
from abc import ABC, abstractmethod
from typing import ClassVar


class ToolError(Exception):
    """工具执行失败，message 会作为 tool_result 回灌给模型"""


# 依赖/缓存目录：Glob 遍历剪枝、Grep 结果过滤共用
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".idea"}


class Tool(ABC):
    name: str = ""
    description: str = ""
    parameters: ClassVar[dict] = {}

    @abstractmethod
    def execute(self, **kwargs) -> str:
        ...


def truncate(text: str, head: int = 15000, tail: int = 5000) -> str:
    if len(text) <= head + tail:
        return text
    return text[:head] + f"\n...[已截断 {len(text) - head - tail} 字符]...\n" + text[-tail:]







