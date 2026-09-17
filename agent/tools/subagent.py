# @File:     subagent.py
# @DateTime: 2026/09/17
"""子代理（P17 M1）：Agent 工具 + spawn 装配。
子循环 = AgentLoop 新实例（独立上下文/单代理预算），共享 provider 客户端；
只读权限（SubGate）、无 Agent 工具（防递归）、输出进 TranscriptSink。
装配用 sandbox.set_executor 同款全局模式：main 启动时 configure()。"""
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from itertools import count
from typing import ClassVar

from ..permissions import SubGate
from ..prompts import load_system_prompt
from .base import Tool, ToolError
from .registry import register

BASE_TOOLS = {"Read", "Grep", "Glob"}

# M2 并发：子代理类工具集合（loop 据此分组进线程池；M3 起 Spawn 派 worker 同组）
CHILD_TOOLS = {"Agent", "Spawn"}
CONSOLE_LOCK = threading.Lock()   # 跨线程单行打印锁：事件行/汇总行防撕裂
MAX_PARALLEL = 3                  # 同回合子代理并发上限（任务 2-3 config 化）

_live = True                      # 同步单代理=True 前缀直印；并发批次=False 只入 transcript


def set_live(value: bool) -> None:
    global _live
    _live = value

_IDENTITY = ("\n\n[子代理身份] 你是主会话派生的子代理：任务由下方任务书完整给定，"
             "看不到主会话历史；不可向用户提问；完成后输出自包含的最终报告"
             "（结论 + 关键证据），主会话只能看到这份报告。")


class TranscriptSink:
    """子代理输出捕获：有界 transcript。
    write=流式文本增量（live=True 同步单代理才带前缀直印，并发只入 transcript）；
    event=工具事件行（一行粒度，并发也经 CONSOLE_LOCK 带前缀直印）。"""

    def __init__(self, agent_id: str, live: bool = False, maxlen: int = 2000,
                 registry=None):
        self.agent_id = agent_id
        self.live = live
        self.lines: deque[str] = deque(maxlen=maxlen)
        self.registry = registry   # SubagentRegistry：行同步进查看器（Ctrl+T / /agents）

    def _emit(self, line: str, force: bool) -> None:
        self.lines.append(line)
        if self.registry is not None:
            self.registry.add_lines(self.agent_id, line)
        if force or self.live:
            with CONSOLE_LOCK:
                print(f"[{self.agent_id}] {line}")

    def write(self, line: str) -> None:
        self._emit(line, force=False)

    def event(self, line: str) -> None:
        self._emit(line, force=True)


@dataclass
class _SpawnCtx:
    provider: object
    parent_gate: object
    max_turns: int
    allow_bash: bool
    token_slice: int
    budget_pool: object
    journal: object
    registry: object = None


_CTX: _SpawnCtx | None = None
_seq = count(1)


def configure(provider, parent_gate, *, max_turns: int = 15, allow_bash: bool = False,
              token_slice: int = 100000, budget_pool=None, journal=None,
              max_parallel: int = 3, registry=None) -> None:
    global _CTX, MAX_PARALLEL
    _CTX = _SpawnCtx(provider=provider, parent_gate=parent_gate, max_turns=max_turns,
                     allow_bash=allow_bash, token_slice=token_slice,
                     budget_pool=budget_pool, journal=journal, registry=registry)
    MAX_PARALLEL = max_parallel


def reset() -> None:
    global _CTX, _seq
    _CTX = None
    _seq = count(1)


def spawn(description: str, prompt: str, *, extra_tools: set[str] | None = None,
          system_note: str = "") -> str:
    """装配并运行一个子代理，返回报告文本（终止/异常也回灌文本，不向上抛）。"""
    if _CTX is None:
        raise ToolError("子代理未启用：main 未完成装配（检查启动日志或 config subagent 配置）")
    from ..loop import AgentLoop  # 延迟导入：loop ↔ tools 本就互相依赖
    n = next(_seq)
    agent_id = f"sub-{n}"
    reg = _CTX.registry
    tools = set(BASE_TOOLS)
    if _CTX.allow_bash:
        tools.add("Bash")
    tools |= extra_tools or set()
    sink = TranscriptSink(agent_id, live=_live, registry=reg)
    loop = AgentLoop(
        provider=_CTX.provider,
        system=load_system_prompt() + _IDENTITY + system_note,
        max_turns=_CTX.max_turns,
        token_budget=_CTX.token_slice,
        permission_gate=SubGate(_CTX.parent_gate, allow_bash=_CTX.allow_bash,
                                extra_tools=extra_tools),
        journal=_CTX.journal,
        allowed_tools=tools,
        sink=sink,
        budget_pool=_CTX.budget_pool,
        agent_id=agent_id,
    )
    if reg is not None:
        reg.register(agent_id, description)
    status = "done"
    try:
        report = loop.run(prompt)
        if "[任务终止]" in report:
            status = "stopped"
    except BaseException:
        status = "error"
        raise
    finally:
        if reg is not None:
            reg.update(agent_id, status=status, ended=datetime.now())
    header = f"[子代理·{description}] 工具调用 {sum(loop.tool_calls.values())} 次"
    return f"{header}\n{report}"


@register
class AgentTool(Tool):
    name = "Agent"
    description = ("派生子代理独立完成任务并返回最终报告。子代理拥有独立上下文窗口（不占用本会话），"
                   "默认只读（Read/Grep/Glob），不能再派生子代理。任务书必须自包含："
                   "写清目标、范围与期望的报告格式。适合大范围检索、证据收集、独立验证；"
                   "写文件等需确认的操作请留在主会话执行。")
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "任务简述（一行，进度显示用）"},
            "prompt": {"type": "string",
                       "description": "完整任务书：目标、范围、期望报告格式（子代理看不到主会话历史）"},
        },
        "required": ["description", "prompt"],
    }

    def execute(self, description: str, prompt: str) -> str:
        return spawn(description, prompt)
