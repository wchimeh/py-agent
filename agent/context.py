# @File:     context.py
# @Author:   mjh
# @DateTime: 2026/03/15/13:57
import json

from .prompts import latest_version, load_prompt
from .providers.base import (
    AssistantMessage,
    LLMResponse,
    Message,
    ToolResultMessage,
    UserMessage,
)

# 压缩提示词：文本在 prompts/compact_v1.md，文件即版本
COMPACT_SYSTEM = load_prompt("compact")
COMPACT_VERSION = latest_version("compact")


def estimate_tokens(text: str) -> int:
    """
    ASCII 4 字符≈1 token，非 ASCII 1 字符≈1 token。宁高估不低估。
    """
    if not text:
        return 0
    non_ascii = sum(1 for c in text if ord(c) > 127)
    return non_ascii + (len(text) - non_ascii + 3) // 4


def estimate_message_tokens(msg: Message) -> int:
    if isinstance(msg, AssistantMessage):
        total = estimate_tokens(msg.text or "")
        for tc in msg.tool_calls:
            total += 8 + estimate_tokens(tc.name) + estimate_tokens(json.dumps(tc.arguments, ensure_ascii=False))
    else:
        total = estimate_tokens(msg.content)
    return total + 4    # 角色等结构开销


class ContextTracker:
    """
    真实锚点 + 增量估算：每次响应后锚定，之后只估算新增消息。
    锚定时按 真实 usage / 朴素估算 计算校准系数 ratio（钳制 0.5~3.0），
    增量部分乘 ratio——不同模型 tokenizer 特性差异由此吸收。
    """
    def __init__(self):
        self.anchor_tokens = 0
        self.anchor_len = 0
        self.ratio = 1.0

    def update_anchor(self, resp: LLMResponse, messages: list[Message]) -> None:
        """
        必须在 append 本轮 AssistantMessage 之前调用：resp.prompt_tokens 是服务端按『发送时的历史』算的真实值。
        服务端流式未回 usage（计 0）时不重锚——保持旧锚点，估算继续可用。
        """
        if resp.prompt_tokens <= 0:
            return
        naive = sum(estimate_message_tokens(m) for m in messages)
        self.ratio = min(3.0, max(0.5, resp.prompt_tokens / max(1, naive)))
        self.anchor_tokens = resp.prompt_tokens
        self.anchor_len = len(messages)

    def reset(self) -> None:
        """
        压缩替换历史后锚点失效；下一轮 chat 会重新锚定。
        """
        self.anchor_tokens = 0
        self.anchor_len = 0
        self.ratio = 1.0

    def estimate(self, messages: list[Message]) -> int:
        """
        计算当前轮 + 增量估算（增量乘校准系数）
        """
        extra = sum(estimate_message_tokens(m) for m in messages[self.anchor_len:])
        return self.anchor_tokens + int(extra * self.ratio)


def split_for_compact(messages: list[Message], keep_recent: int) -> tuple[list[Message], list[Message]]:
    """
    切分为 (old_part, kept_part)。
    kept 开头不能是孤儿 tool_result（其配对 tool_use 在 old 里会被 anthropic 400），
    此时把 cut 前移，让对应的 assistant(tool_use) 一起进 old。
    """
    cut = max(1, len(messages) - keep_recent)
    while cut > 1 and isinstance(messages[cut], ToolResultMessage):
        cut -= 1
    return messages[:cut], messages[cut:]


def render_for_summary(old_part: list[Message]) -> str:
    """
    把消息渲染为摘要请求的纯文本输入。
    """
    lines = []
    for m in old_part:
        if isinstance(m, UserMessage):
            lines.append(f"[用户] {m.content}")
        elif isinstance(m, AssistantMessage):
            if m.text:
                lines.append(f"[助手] {m.text}")
            for tc in m.tool_calls:
                args = " ".join(f"{k}={str(v)[:60]}" for k, v in tc.arguments.items())
                lines.append(f"[助手调用工具] {tc.name}({args})")
        else:
            tag = "工具结果(错误)" if m.is_error else "工具结果"
            lines.append(f"[{tag}] {m.content[:300]}")
    return "\n".join(lines)
