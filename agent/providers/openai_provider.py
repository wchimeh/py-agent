# @File:     openai_provider.py
# @Author:   mjh
# @DateTime: 2026/03/14/15:02

import json
import re

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


_THINK_OPEN, _THINK_CLOSE = "<think>", "</think>"
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class _ThinkFilter:
    """流式剥离 content 内嵌的 <think>…</think>（推理模型思考段，标签可能跨 chunk 拆开）。"""

    def __init__(self):
        self._in_think = False
        self._pending = ""

    def feed(self, delta: str) -> str:
        self._pending += delta
        out = []
        while self._pending:
            tag = _THINK_CLOSE if self._in_think else _THINK_OPEN
            i = self._pending.find(tag)
            if i >= 0:
                if not self._in_think:
                    out.append(self._pending[:i])
                self._pending = self._pending[i + len(tag):]
                self._in_think = not self._in_think
                continue
            keep = self._holdback(tag)
            if not self._in_think:          # 思考侧内容直接吞，不上屏
                out.append(self._pending[:keep])
            self._pending = self._pending[keep:]
            break
        return "".join(out)

    def flush(self) -> str:
        """流结束：放行被 holdback 的正文残留；未闭合的思考段丢弃。"""
        rest, self._pending = self._pending, ""
        return "" if self._in_think else rest

    def _holdback(self, tag: str) -> int:
        """可安全放行长度：尾部疑似标签前缀的部分留缓冲等下一 chunk。"""
        for k in range(min(len(tag) - 1, len(self._pending)), 0, -1):
            if self._pending.endswith(tag[:k]):
                return len(self._pending) - k
        return len(self._pending)


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
        text = _THINK_RE.sub("", choice.message.content or "").strip()
        return LLMResponse(
            text=text or None, tool_calls=tool_calls,
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
        flt = _ThinkFilter()
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
                piece = flt.feed(delta.content)
                if piece:
                    texts.append(piece)
                    on_text(piece)
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
        tail = flt.flush()             # 流结束：放行被 holdback 的正文残留
        if tail:
            texts.append(tail)
            on_text(tail)
        return LLMResponse(
            text="".join(texts).strip() or None, tool_calls=tool_calls,
            stop_reason=_FINISH_MAP.get(finish, StopReason.END_TURN),
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0)




