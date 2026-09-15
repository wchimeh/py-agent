# -*- coding: utf-8 -*-
# @File:     permissions.py
# @Author:   mjh
# @DateTime: 2026/03/15/10:57

import re
from enum import Enum

from prompt_toolkit import prompt

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

    def __init__(self, mode: str | PermissionMode = PermissionMode.DEFAULT, ask_fn=default_ask):
        self.mode = PermissionMode(mode)  # 非法值抛 ValueError，由 main 捕获提示
        self.ask_fn = ask_fn
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

