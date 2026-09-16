# @File:     base.py
# @Author:   mjh
# @DateTime: 2026/03/14/13:39

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# provider 与 tools.registry 共用的畸形参数哨兵：arguments 的 JSON 解析失败时，
# provider 把原始片段塞进此键，registry 命中后按解析错误回灌模型
MALFORMED_ARGS_KEY = "__malformed_json__"


class StopReason(str, Enum):
    END_TURN = 'end_turn'
    TOOL_USE = 'tool_use'
    MAX_TOKENS = 'max_tokens'
    ERROR = 'error'


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class UserMessage:
    content: str
    role: str = 'user'


@dataclass
class AssistantMessage:
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    role: str = 'assistant'


@dataclass
class ToolResultMessage:
    tool_call_id: str
    content: str
    is_error: bool = False
    role: str = 'tool'


Message = UserMessage | AssistantMessage | ToolResultMessage


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict


@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall]
    stop_reason: StopReason
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMProvider(ABC):

    @abstractmethod
    def chat(self, messages: list[Message], tools: list[ToolDef] | None = None, system: str = "") -> LLMResponse:
        ...

    def chat_stream(self, messages: list[Message], tools: list[ToolDef] | None = None,
                    system: str = "", on_text=None) -> LLMResponse:
        """流式接口：on_text 逐段接收文本增量。默认非流式降级（on_text 不会被调用）。"""
        return self.chat(messages, tools, system)









