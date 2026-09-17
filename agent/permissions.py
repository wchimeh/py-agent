# @File:     permissions.py
# @Author:   mjh
# @DateTime: 2026/03/15/10:57

import re
from enum import Enum

from prompt_toolkit import prompt

from . import sandbox
from .providers.base import ToolCall


class PermissionMode(str, Enum):
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    BYPASS = "bypass"


READ_ONLY_TOOLS = {"Read", "Glob", "Grep"}
EDIT_TOOLS = {"Write", "Edit"}

# 警示增强清单（非安全边界）：命中只在询问时加 ⚠，宁误报不漏报
DANGEROUS_PATTERNS = [
  (r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)\b", "rm -rf 递归强删"),
  (r"\bdel\s+(/[sq]\s+)+", "del /s /q 递归删除"),
  (r"\brd\s+/s", "rd /s 删除目录树"),
  (r"(^|\s)format\s+[a-zA-Z]:", "格式化磁盘"),
  (r"\bshutdown\b", "关机/重启"),
  (r"git\s+push\s+.*(--force\b|(?<!\w)-f\b)", "git push 强制推送"),
  (r"\b(curl|wget)\b[^|]*\|\s*(sh|bash|python)", "下载内容直接进 shell"),
  (r"\breg\s+(add|delete)\b", "修改注册表"),
  (r"\bdiskpart\b|\bmkfs\b", "磁盘分区/格式化"),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), label) for p, label in DANGEROUS_PATTERNS]


def match_danger(command: str) -> str | None:
    for rx, label in _COMPILED:
        if rx.search(command):
            return label
    return None


def default_ask(tc: ToolCall, danger: str | None) -> str:
    summary = " ".join(f"{k}={str(v)[:60]!r}"
                       for k, v in list(tc.arguments.items())[:2])
    print(f"  [权限] {tc.name} {summary}")
    if danger:
        print(f"  ⚠ 危险命令模式命中: {danger}")
    while True:
        answer = prompt("  允许？[y]本次 [a]会话内总是 [n]拒绝 [e]会话内总是拒绝: ").strip().lower()
        if answer in ("y", "a", "n", "e"):
            return answer


class PermissionGate:

    def __init__(self, mode: str | PermissionMode = PermissionMode.DEFAULT, ask_fn=default_ask,
                 sandbox_trusted: bool = False):
        self.mode = PermissionMode(mode)  # 非法值抛 ValueError，由 main 捕获提示
        self.ask_fn = ask_fn
        self.sandbox_trusted = sandbox_trusted  # docker 沙箱内非危险 Bash 免审批
        self.always: set[str] = set()  # 会话内总是允许
        self.never: set[str] = set()  # 会话内总是拒绝

    def set_mode(self, mode: str):
        self.mode = PermissionMode(mode)

    def _key(self, tc: ToolCall) -> str:
        if tc.name == "Bash":
            parts = str(tc.arguments.get("command", "")).split()
            return f"Bash:{parts[0].lower()}" if parts else "Bash:"
        return tc.name

    def authorize(self, tc: ToolCall) -> tuple[bool, str]:
        if tc.name in READ_ONLY_TOOLS:
            return True, ""
        if self.mode is PermissionMode.BYPASS:
            return True, ""

        key = self._key(tc)
        if key in self.never:
            return False, f"用户已设置会话内拒绝 {key}，请调整方案或向用户说明意图"

        # 危险命令在会话记忆之前检查：防止 a 允许 cd 后，cd .. && del /s 借道直通
        danger = match_danger(str(tc.arguments.get("command", ""))) if tc.name == "Bash" else None
        if key in self.always and not danger:
            return True, ""

        # docker 沙箱内非危险 Bash 免审批（借鉴 Claude Code autoAllowBashIfSandboxed）。
        # 危险命令不豁免：工作区是 rw 挂载，rm -rf /workspace 真能删宿主文件。
        if (tc.name == "Bash" and self.sandbox_trusted and not danger
                and isinstance(sandbox.get_executor(), sandbox.DockerExecutor)):
            return True, ""

        if tc.name in EDIT_TOOLS:
            needs_ask = self.mode is PermissionMode.DEFAULT
        else:  # Bash 及未知工具（保守起见都要问）
            needs_ask = True
        if not needs_ask:
            return True, ""

        answer = self.ask_fn(tc, danger)
        if answer == "y":
            return True, ""
        if answer == "a":
            self.always.add(key)
            return True, ""
        if answer == "e":
            self.never.add(key)
        return False, f"用户拒绝了 {tc.name} 调用（{key}），请调整方案或向用户说明意图"


class SubGate:
    """子代理权限门：非交互（永不弹询问），只读为默认。
    Write/Edit 一律拒（写操作回主会话）；Bash 仅当 allow_bash 且父会话已"总是允许"
    该命令前缀且非危险模式时放行；父 bypass 不放宽（子代理永远无写权）。
    extra_tools：worker 场景追加放行（任务板工具等，发起方白名单已含）。"""

    def __init__(self, parent: "PermissionGate", allow_bash: bool = False,
                 extra_tools: set[str] | None = None):
        self.parent = parent
        self.allow_bash = allow_bash
        self.extra = set(extra_tools or ())

    def authorize(self, tc: ToolCall) -> tuple[bool, str]:
        if tc.name in READ_ONLY_TOOLS or tc.name in self.extra:
            return True, ""
        if tc.name in EDIT_TOOLS:
            return False, ("子代理为只读模式，无权写文件（Write/Edit）："
                           "请在报告中说明要做的修改，由主会话执行")
        if tc.name == "Bash":
            danger = match_danger(str(tc.arguments.get("command", "")))
            if danger:
                return False, f"子代理无法执行危险命令（{danger}）：请在主会话确认执行"
            if not self.allow_bash:
                return False, ("子代理未启用 Bash（config subagent.allow_bash）"
                               "且非交互无法询问：请在主会话执行")
            if self.parent._key(tc) in self.parent.always:
                return True, ""
            return False, ("子代理非交互，仅可执行主会话已'总是允许'的命令前缀："
                           "其余命令请在主会话确认")
        return False, f"子代理不可用工具 {tc.name}（可用工具由发起方白名单限定）"

