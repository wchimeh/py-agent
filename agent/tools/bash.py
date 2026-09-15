# -*- coding: utf-8 -*-
# @File:     bash.py
# @Author:   mjh
# @DateTime: 2026/03/14/16:58
import subprocess
from .base import Tool, ToolError, truncate
from .registry import register


@register
class BashTool(Tool):
    name = "Bash"
    description = "执行 shell 命令。当前环境 Windows/cmd.exe，优先用跨平台命令。"
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "timeout": {"type": "integer", "description": "超时秒数，默认 120"},
        },
        "required": ["command"],
    }

    def execute(self, command: str, timeout: int = 120) -> str:
        try:
            r = subprocess.run(command, shell=True, capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ToolError(f"命令超时（>{timeout}s），已终止")
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if err:
            out = f"{out}\n[stderr]\n{err}" if out else f"[stderr]\n{err}"
        out = truncate(out or "(无输出)")
        if r.returncode != 0:
            # 失败也带上完整输出：模型需要看到报错才能自愈
            raise ToolError(f"[退出码 {r.returncode}]\n{out}")
        return out
