# @File:     loop.py
# @Author:   mjh
# @DateTime: 2026/03/14/15:22

import sys
import time
from concurrent.futures import ThreadPoolExecutor

from .context import (
    COMPACT_SYSTEM,
    COMPACT_VERSION,
    ContextTracker,
    render_for_summary,
    split_for_compact,
)
from .permissions import PermissionGate
from .prompts import latest_version, load_system_prompt
from .providers.base import (
    AssistantMessage,
    LLMProvider,
    Message,
    StopReason,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from .spinner import Spinner
from .tools import check_tool_call, execute_tool, get_tool_defs
from .tools.subagent import CHILD_TOOLS, CONSOLE_LOCK, set_live

# 系统提示词：文本在 prompts/system_v1.md，文件即版本（常量保留仅为兼容）
SYSTEM_PROMPT = load_system_prompt()


def _arg_preview(v, limit=60) -> str:
    """工具行参数预览：≤60 字符全显（覆盖常见 Windows 路径）；超长截断并标注原始长度。"""
    s = str(v)
    if len(s) <= limit:
        return repr(s)
    return f"{s[:limit]!r}(len={len(s)})"


class AgentLoop:
    def __init__(self, provider: LLMProvider, system: str | None = None,
                 prompt_version: int | None = None,
                 max_turns: int = 25, token_budget: int = 500000,
                 permission_gate: PermissionGate | None = None,
                 context_window: int = 128000, compact_threshold: float = 0.8,
                 keep_recent: int = 8,
                 journal=None,
                 allowed_tools: set[str] | None = None,
                 sink=None,
                 budget_pool=None,
                 agent_id: str = "main",
                 ):
        self.provider = provider
        if system is None:
            system = load_system_prompt(prompt_version)
            self.prompt_version = prompt_version if prompt_version is not None else latest_version("system")
        else:
            self.prompt_version = "custom"
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
        self.allowed_tools = allowed_tools    # None = 注册表全部（主循环默认）
        self.sink = sink                      # None = 直印 stdout；子代理传 transcript sink
        self.budget_pool = budget_pool        # 进程级 token 总闸（None = 不启用）
        self.agent_id = agent_id
        self.tasks_total = 0
        self.tool_calls: dict[str, int] = {}
        self.last_streamed = False    # 末回合最终文本是否已流式打印（main 据此决定是否重印）
        self._streamed_turn = False

    def _say(self, line: str, event: bool = False) -> None:
        """输出一行：event=True 为工具/系统事件行（子代理并发时仍带前缀直印），
        其余为流式文本增量（并发时只入 transcript 防混流）。"""
        if self.sink is not None:
            if event:
                self.sink.event(line)
            else:
                self.sink.write(line)
        else:
            print(line)

    def run(self, user_input: str) -> str:
        self.messages.append(UserMessage(content=user_input))
        self.tasks_total += 1
        self.last_streamed = False
        self._log("task_start", input=user_input[:80], prompt=self.prompt_version)
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
            self._log("llm_call", prompt_tokens=resp.prompt_tokens,
                      completion=resp.completion_tokens,
                      secs=round(secs, 2), stop=resp.stop_reason.value,
                      streamed=self._streamed_turn)
            task_tokens += resp.prompt_tokens + resp.completion_tokens
            if self.budget_pool is not None and not self.budget_pool.consume(
                    resp.prompt_tokens + resp.completion_tokens):
                return (f"[任务终止] 原因：进程 token 预算池已耗尽"
                        f"（已用 {self.budget_pool.used}/{self.budget_pool.total}），强制终止")
            self.messages.append(AssistantMessage(text=resp.text,
                                                  tool_calls=resp.tool_calls))
            if resp.stop_reason != StopReason.TOOL_USE:
                self.last_streamed = self._streamed_turn
                if resp.stop_reason == StopReason.MAX_TOKENS:
                    if self._streamed_turn:   # 全文已流式打印，提示直接上屏
                        self._say("\n[系统提示] 回答因达到 max_tokens 被截断，可调大 config.yaml 的 max_tokens")
                        return resp.text or ""
                    return (resp.text or "") + \
                        "\n\n[系统提示] 回答因达到 max_tokens 被截断，可调大 config.yaml 的 max_tokens"
                return resp.text or ""
            turns += 1
            if turns > self.max_turns:
                return f"[任务终止] 原因：已达单任务工具往返上限（{self.max_turns}），强制终止"
            if task_tokens > self.token_budget:
                return f"[任务终止] 原因：已达 token 预算（{task_tokens}/{self.token_budget}）"
            self._run_tools(resp.tool_calls)  # 必须全应答，漏一个 anthropic 会 400
            # 不 return，继续循环让模型消化工具结果

    def _log(self, event: str, **fields) -> None:
        if self.journal:
            self.journal.log(event, agent=self.agent_id, **fields)

    def _call_llm(self):
        """有 chat_stream 能力则流式（文本增量直接打印），否则转圈等待（测试 Fake 走此路径）。
        sink 模式（子代理）：无转圈、增量与提示全部进 sink 不占 stdout。"""
        tools = get_tool_defs(only=self.allowed_tools)
        stream_fn = getattr(self.provider, "chat_stream", None)
        if stream_fn is None:
            if self.sink is not None:
                return self.provider.chat(self.messages, tools=tools, system=self.system)
            with Spinner("思考中"):
                return self.provider.chat(self.messages, tools=tools, system=self.system)
        if self.sink is not None:
            return stream_fn(self.messages, tools=tools, system=self.system,
                             on_text=lambda t: self.sink.write(t))
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
            if self.sink is not None:
                resp = self.provider.chat([UserMessage(content=render_for_summary(old))], tools=None, system=COMPACT_SYSTEM)
            else:
                with Spinner("压缩上下文"):
                    resp = self.provider.chat([UserMessage(content=render_for_summary(old))], tools=None, system=COMPACT_SYSTEM)
            self._account(resp, time.monotonic() - t0)  # 摘要开销如实计入统计
            head = f"[会话摘要]\n{resp.text or ''}"
        except Exception as e:  # 摘要失败降级硬截断；KI 是 BaseException 不受影响
            degraded = True
            self._say(f"  [context] ⚠ 摘要失败（{type(e).__name__}），降级为截断")
            head = f"[会话摘要·截断] 历史过长摘要失败，已丢弃更早的 {len(old)} 条，仅保留最近 {len(kept)} 条。此前任务目标请向用户确认。"
        self.messages = [UserMessage(content=head), *kept]  # 关键：替换历史
        self.tracker.reset()  # 关键：锚点失效
        self.compact_count += 1
        self._log("compact", msgs_before=len(old) + len(kept),
                  msgs_after=len(self.messages), degraded=degraded,
                  prompt=COMPACT_VERSION)
        self._say(f"  [context] 已压缩：{len(old) + len(kept)} → {len(self.messages)} 条（估算 {before} → {self.tracker.estimate(self.messages)} tokens）")

    def _run_tools(self, calls: list[ToolCall]) -> None:
        """同回合工具分组执行：≥2 个子代理类工具进线程池并发，其余（含单个子代理）串行照旧。"""
        child = [tc for tc in calls if tc.name in CHILD_TOOLS]
        if len(child) < 2:
            for tc in calls:
                self._run_tool(tc)
            return
        for tc in calls:
            if tc.name not in CHILD_TOOLS:
                self._run_tool(tc)
        self._dispatch_children(child)

    def _preflight(self, tc: ToolCall) -> str | None:
        """白名单/校验/权限三重预检（须在主线程做：authorize 可能弹询问）。
        返回错误 note（None=放行）。"""
        if self.allowed_tools is not None and tc.name not in self.allowed_tools:
            self._log("denied", tool=tc.name, reason="not_in_allowed_tools")
            return (f"工具 {tc.name} 不在本代理可用工具列表内"
                    f"（可用：{', '.join(sorted(self.allowed_tools))}）")
        err = check_tool_call(tc)   # 无效调用（未知/畸形/校验失败）不弹权限框
        if err:
            self._log("invalid_tool_call", tool=tc.name)
            return err
        allowed, note = self.gate.authorize(tc)
        if not allowed:
            self._log("denied", tool=tc.name)
            return note
        return None

    def _run_tool(self, tc: ToolCall):
        args = " ".join(f"{k}={_arg_preview(v)}"
                        for k, v in list(tc.arguments.items())[:3])
        self._say(f"● {tc.name}({args})", event=True)

        note = self._preflight(tc)
        if note:
            self.messages.append(ToolResultMessage(tc.id, note, is_error=True))
            self._say(f"  ✗ {note.splitlines()[0][:100]}", event=True)
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
        self._say(line, event=True)
        self.messages.append(ToolResultMessage(tc.id, content, is_error))

    def _dispatch_children(self, calls: list[ToolCall]) -> None:
        """并发派发子代理类工具：预检在主线程（权限可能询问），执行进线程池，
        结果按原 tc 顺序回灌；并发期间子代理事件行经 console 锁带前缀直印。"""
        self._say(f"[agent×{len(calls)}] 并发派发 {len(calls)} 个子代理", event=True)
        ready = []
        for tc in calls:
            args = " ".join(f"{k}={_arg_preview(v)}"
                            for k, v in list(tc.arguments.items())[:3])
            self._say(f"● {tc.name}({args})", event=True)
            note = self._preflight(tc)
            if note is None:
                ready.append(tc)
            else:
                self.messages.append(ToolResultMessage(tc.id, note, is_error=True))
                self._say(f"  ✗ {note.splitlines()[0][:100]}", event=True)
        if not ready:
            return
        set_live(False)   # 并发模式：子代理流式文本只入 transcript，事件行仍前缀直印
        try:
            from .tools import subagent as _sa  # 运行时读取：configure 可更新并发上限
            with ThreadPoolExecutor(max_workers=min(len(ready), _sa.MAX_PARALLEL)) as ex:
                results = list(ex.map(self._exec_child, ready))
        finally:
            set_live(True)
        for i, (tc, (content, is_error, secs)) in enumerate(zip(ready, results, strict=True), 1):
            mark = "✗" if is_error else "✓"
            desc = str(tc.arguments.get("description", ""))[:30]
            with CONSOLE_LOCK:
                print(f"[{i}/{len(ready)}] {desc} {mark} {secs:.1f}s")
            self.tool_calls[tc.name] = self.tool_calls.get(tc.name, 0) + 1
            self._log("tool_call", name=tc.name, is_error=is_error, secs=round(secs, 2))
            self.messages.append(ToolResultMessage(tc.id, content, is_error))

    def _exec_child(self, tc: ToolCall) -> tuple[str, bool, float]:
        t0 = time.monotonic()
        try:
            content, is_error = execute_tool(tc)   # spawn 兜底回灌文本，通常不抛
        except Exception as e:                     # 防御：异常也回灌，不毁整批
            content, is_error = f"[子代理异常] {type(e).__name__}: {e}", True
        return content, is_error, time.monotonic() - t0
