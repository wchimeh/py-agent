# -*- coding: utf-8 -*-
# @File:     anthropic_provider.py
# @Author:   mjh
# @DateTime: 2026/03/14/15:17
from anthropic import Anthropic
from .base import LLMProvider, LLMResponse, StopReason, ToolCall, UserMessage, AssistantMessage, ToolResultMessage

_STOP_MAP = {"end_turn": StopReason.END_TURN, "tool_use": StopReason.TOOL_USE, "max_tokens": StopReason.MAX_TOKENS}


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str, api_key: str, base_url: str, max_tokens: int = 4096, timeout: int = 120):
        self.model = model
        self.max_tokens = max_tokens
        self.client = Anthropic(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)

    def _convert(self, messages) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if isinstance(m, UserMessage):
                out.append({"role": "user", "content": m.content})
            elif isinstance(m, AssistantMessage):
                blocks = []
                if m.text:
                    blocks.append({"type": "text", "text": m.text})
                for tc in m.tool_calls:
                    blocks.append({"type": "tool_use", "id": tc.id,
                                   "name": tc.name, "input": tc.arguments})
                out.append({"role": "assistant", "content": blocks})
            elif isinstance(m, ToolResultMessage):
                block = {"type": "tool_result", "tool_use_id": m.tool_call_id,
                         "content": m.content}
                if m.is_error:
                    block["is_error"] = True
                # 关键：同一批工具结果必须合并在同一条 user 消息里
                if out and out[-1]["role"] == "user" and \
                        isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return out

    def _kwargs(self, messages, tools, system) -> dict:
        kwargs: dict = {
            "model": self.model, "max_tokens": self.max_tokens,
            "messages": self._convert(messages)}       # system 不进 messages
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [{"name": t.name, "description": t.description,
                                "input_schema": t.parameters} for t in tools]
        return kwargs

    @staticmethod
    def _parse(resp) -> LLMResponse:
        texts, tool_calls = [], []
        for block in resp.content:
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name,
                                           arguments=dict(block.input)))
        return LLMResponse(
            text="\n".join(texts) or None, tool_calls=tool_calls,
            stop_reason=_STOP_MAP.get(resp.stop_reason, StopReason.END_TURN),
            prompt_tokens=resp.usage.input_tokens,
            completion_tokens=resp.usage.output_tokens)

    def chat(self, messages, tools=None, system=""):
        return self._parse(self.client.messages.create(**self._kwargs(messages, tools, system)))

    def chat_stream(self, messages, tools=None, system="", on_text=None):
        if on_text is None:
            return self.chat(messages, tools, system)
        # tool_use 的增量累积（input_json_delta）由 SDK 在 final_message 里组好
        with self.client.messages.stream(**self._kwargs(messages, tools, system)) as s:
            for delta in s.text_stream:
                on_text(delta)
            final = s.get_final_message()
        return self._parse(final)
