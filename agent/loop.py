# -*- coding: utf-8 -*-
# @File:     loop.py
# @Author:   mjh
# @DateTime: 2026/03/14/15:22

import sys
import time
from .spinner import Spinner
from .providers.base import LLMProvider, Message, StopReason, UserMessage, AssistantMessage, ToolResultMessage, ToolCall
from .tools import get_tool_defs, execute_tool
from .permissions import PermissionGate
from .context import COMPACT_SYSTEM, ContextTracker, render_for_summary, split_for_compact
from .journal import Journal


SYSTEM_PROMPT = """你是运行在终端里的编码助手，可以调用工具完成实际任务。
  规则：
  - 文件操作一律使用绝对路径；Edit 之前先 Read 确认原文
  - 当前环境是 Windows，shell 走 cmd.exe，命令优先跨平台
  - 工具失败时阅读错误信息自行修正，不要原样重试
  """


class AgentLoop:
    def __init__(self, provider: LLMProvider, system: str = SYSTEM_PROMPT,
                 max_turns: int = 25, token_budget: int = 500000,
                 permission_gate: PermissionGate | None = None,
                 context_window: int = 128000, compact_threshold: float = 0.8,
                 keep_recent: int = 8,
                 journal=None,
                 ):
        self.provider = provider
        self.system = system
        self.messages: list[Message] = []
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.last_usage = None
        self.max_turns = max_turns
        self.token_budget = token_budget
        self.gate = permission_gate or PermissionGate()

        self.context_window = context_window  # 0 = 关闭压缩
        self.compact_threshold = compact_threshold
        self.keep_recent = keep_recent
        self.tracker = ContextTracker()
        self.compact_count = 0

        self.journal = journal
        self.tasks_total = 0
        self.tool_calls: dict[str, int] = {}
        self.last_streamed = False    # 末回合最终文本是否已流式打印（main 据此决定是否重印）
        self._streamed_turn = False

    def run(self, user_input: str) -> str:
        self.messages.append(UserMessage(content=user_input))
        self.tasks_total += 1
        self.last_streamed = False
        self._log("task_start", input=user_input[:80])
        result = self._run_loop()
        self._log("task_end", output=result[:80])
        return result

    def _run_loop(self) -> str:
        turns = 0
        task_tokens = 0  # 任务级：每次 run() 重新累计
        while True:
            if self.context_window and self.tracker.estimate(self.messages) > self.context_window * self.compact_threshold:
                self._compact()
            t0 = time.monotonic()
            self._streamed_turn = False
            resp = self._call_llm()

            self.tracker.update_anchor(resp, self.messages)     # 上下文管理
            secs = time.monotonic() - t0
            self._account(resp, secs)
            self._log("llm_call", prompt=resp.prompt_tokens,
                      completion=resp.completion_tokens,
                      secs=round(secs, 2), stop=resp.stop_reason.value,
                      streamed=self._streamed_turn)
            task_tokens += resp.prompt_tokens + resp.completion_tokens
            self.messages.append(AssistantMessage(text=resp.text,
                                                  tool_calls=resp.tool_calls))
            if resp.stop_reason != StopReason.TOOL_USE:
                self.last_streamed = self._streamed_turn
                if resp.stop_reason == StopReason.MAX_TOKENS:
                    if self._streamed_turn:   # 全文已流式打印，提示直接上屏
                        print("\n[系统提示] 回答因达到 max_tokens 被截断，可调大 config.yaml 的 max_tokens")
                        return resp.text or ""
                    return (resp.text or "") + \
                        "\n\n[系统提示] 回答因达到 max_tokens 被截断，可调大 config.yaml 的 max_tokens"
                return resp.text or ""
            turns += 1
            if turns > self.max_turns:
                return f"[任务终止] 原因：已达单任务工具往返上限（{self.max_turns}），强制终止"
            if task_tokens > self.token_budget:
                return f"[任务终止] 原因：已达 token 预算（{task_tokens}/{self.token_budget}）"
            for tc in resp.tool_calls:  # 必须全应答，漏一个 anthropic 会 400
                self._run_tool(tc)
            # 不 return，继续循环让模型消化工具结果

    def _log(self, event: str, **fields) -> None:
        if self.journal:
            self.journal.log(event, **fields)

    def _call_llm(self):
        """有 chat_stream 能力则流式（文本增量直接打印），否则转圈等待（测试 Fake 走此路径）。"""
        stream_fn = getattr(self.provider, "chat_stream", None)
        if stream_fn is None:
            with Spinner("思考中"):
                return self.provider.chat(self.messages, tools=get_tool_defs(), system=self.system)
        sp = Spinner("思考中")
        sp.__enter__()

        def _on_delta(text: str):
            sp.stop()                    # 首个增量即停转（幂等）
            self._streamed_turn = True
            sys.stdout.write(text)
            sys.stdout.flush()

        try:
            return stream_fn(self.messages, tools=get_tool_defs(),
                             system=self.system, on_text=_on_delta)
        finally:
            sp.__exit__(None, None, None)
            if self._streamed_turn:
                print()

    def _account(self, resp, secs: float):
        self.total_prompt_tokens += resp.prompt_tokens
        self.total_completion_tokens += resp.completion_tokens
        self.last_usage = (resp.prompt_tokens, resp.completion_tokens, secs)

    def _compact(self):
        old, kept = split_for_compact(self.messages, self.keep_recent)
        before = self.tracker.estimate(self.messages)
        degraded = False
        try:
            t0 = time.monotonic()
            with Spinner("压缩上下文"):
                resp = self.provider.chat([UserMessage(content=render_for_summary(old))], tools=None, system=COMPACT_SYSTEM)
            self._account(resp, time.monotonic() - t0)  # 摘要开销如实计入统计
            head = f"[会话摘要]\n{resp.text or ''}"
        except Exception as e:  # 摘要失败降级硬截断；KI 是 BaseException 不受影响
            degraded = True
            print(f"  [context] ⚠ 摘要失败（{type(e).__name__}），降级为截断")
            head = f"[会话摘要·截断] 历史过长摘要失败，已丢弃更早的 {len(old)} 条，仅保留最近 {len(kept)} 条。此前任务目标请向用户确认。"
        self.messages = [UserMessage(content=head)] + kept  # 关键：替换历史
        self.tracker.reset()  # 关键：锚点失效
        self.compact_count += 1
        self._log("compact", msgs_before=len(old) + len(kept),
                  msgs_after=len(self.messages), degraded=degraded)
        print(f"  [context] 已压缩：{len(old) + len(kept)} → {len(self.messages)} 条（估算 {before} → {self.tracker.estimate(self.messages)} tokens）")

    def _run_tool(self, tc: ToolCall):
        args = " ".join(f"{k}={str(v)[:40]!r}"
                        for k, v in list(tc.arguments.items())[:2])
        print(f"● {tc.name}({args})")

        allowed, note = self.gate.authorize(tc)

        if not allowed:
            self.messages.append(ToolResultMessage(tc.id, note, is_error=True))
            self._log("denied", tool=tc.name)
            print(f"  ✗ 已拒绝 · {note[:80]}")
            return

        t0 = time.monotonic()
        content, is_error = execute_tool(tc)
        self.tool_calls[tc.name] = self.tool_calls.get(tc.name, 0) + 1
        self._log("tool_call", name=tc.name, is_error=is_error,
                  secs=round(time.monotonic() - t0, 2))
        mark = "✗" if is_error else "✓"
        line = f"  {mark} {time.monotonic() - t0:.1f}s"
        if is_error:
            line += f" · {content.splitlines()[0][:100]}"
        print(line)
        self.messages.append(ToolResultMessage(tc.id, content, is_error))
