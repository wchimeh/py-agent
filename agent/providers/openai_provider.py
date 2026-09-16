# @File:     openai_provider.py
# @Author:   mjh
# @DateTime: 2026/03/14/15:02

import json

from openai import BadRequestError, OpenAI

from .base import (
    MALFORMED_ARGS_KEY,
    AssistantMessage,
    LLMProvider,
    LLMResponse,
    StopReason,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

_FINISH_MAP = {'stop': StopReason.END_TURN, 'tool_calls': StopReason.TOOL_USE, 'length': StopReason.MAX_TOKENS}


def _parse_args(raw: str | None) -> dict:
    """模型输出的 arguments JSON 解析；非法时塞哨兵片段，由 registry 转为错误回灌。"""
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {MALFORMED_ARGS_KEY: (raw or "")[:500]}


class OpenAIProvider(LLMProvider):

    def __init__(self, model: str, api_key: str, base_url: str, max_tokens: int = 4096, timeout: int = 120):
        self.model = model
        self.max_tokens = max_tokens
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)

    def _convert(self, messages, system: str) -> list[dict]:
        out = list()
        if system:
            out.append({"role": "system", "content": system})
        for m in messages:
            if isinstance(m, UserMessage):
                out.append({"role": "user", "content": m.content})
            elif isinstance(m, AssistantMessage):
                msg: dict = {"role": "assistant", "content": m.text}
                if m.tool_calls:
                    msg["tool_calls"] = [{
                        "id": tc.id, "type": "function",
                        "function": {"name": tc.name,
                                     # OpenAI 的 arguments 是 JSON 字符串
                                     "arguments": json.dumps(tc.arguments, ensure_ascii=False)}}
                        for tc in m.tool_calls]
                out.append(msg)
            elif isinstance(m, ToolResultMessage):
                out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        return out

    def _kwargs(self, messages, tools, system) -> dict:
        kwargs: dict = {
            "model": self.model, "max_tokens": self.max_tokens,
            "messages": self._convert(messages, system)}
        if tools:
            kwargs["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description,
                "parameters": t.parameters}} for t in tools]
        return kwargs

    def chat(self, messages, tools=None, system=""):
        resp = self.client.chat.completions.create(**self._kwargs(messages, tools, system))
        choice = resp.choices[0]
        tool_calls = [ToolCall(id=tc.id, name=tc.function.name,
                               arguments=_parse_args(tc.function.arguments))
                      for tc in (choice.message.tool_calls or [])]
        usage = getattr(resp, "usage", None)
        return LLMResponse(
            text=choice.message.content, tool_calls=tool_calls,
            stop_reason=_FINISH_MAP.get(choice.finish_reason, StopReason.END_TURN),
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0)

    def chat_stream(self, messages, tools=None, system="", on_text=None):
        if on_text is None:
            return self.chat(messages, tools, system)
        kwargs = self._kwargs(messages, tools, system)
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}
        try:
            chunks = self.client.chat.completions.create(**kwargs)
        except BadRequestError:
            # 兼容服务不支持 stream_options：400 发生在流开始前，安全降级非流式
            return self.chat(messages, tools, system)
        texts: list[str] = []
        acc: dict[int, dict] = {}      # index -> {"id","name","args"}
        finish, usage = None, None
        for ch in chunks:
            if getattr(ch, "usage", None):
                usage = ch.usage
            if not ch.choices:
                continue               # 仅含 usage 的收尾块
            c = ch.choices[0]
            delta = c.delta
            if delta and delta.content:
                texts.append(delta.content)
                on_text(delta.content)
            if delta and delta.tool_calls:
                for d in delta.tool_calls:
                    slot = acc.setdefault(d.index, {"id": None, "name": None, "args": ""})
                    if d.id:
                        slot["id"] = d.id
                    fn = getattr(d, "function", None)
                    if fn and fn.name:
                        slot["name"] = fn.name
                    if fn and fn.arguments:
                        slot["args"] += fn.arguments
            if c.finish_reason:
                finish = c.finish_reason
        tool_calls = [ToolCall(id=s["id"] or f"t{i}", name=s["name"] or "",
                               arguments=_parse_args(s["args"]))
                      for i, s in sorted(acc.items())]
        return LLMResponse(
            text="".join(texts) or None, tool_calls=tool_calls,
            stop_reason=_FINISH_MAP.get(finish, StopReason.END_TURN),
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0)




