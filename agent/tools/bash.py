# @File:     bash.py
# @Author:   mjh
# @DateTime: 2026/03/14/16:58
import subprocess
from typing import ClassVar

from .. import sandbox
from .base import Tool, ToolError, truncate
from .registry import register

DEFAULT_DESCRIPTION = "执行 shell 命令。当前环境 Windows/cmd.exe，优先用跨平台命令。"


@register
class BashTool(Tool):
    name = "Bash"
    description = DEFAULT_DESCRIPTION
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "timeout": {"type": "integer", "description": "超时秒数，默认 120"},
        },
        "required": ["command"],
    }

    def execute(self, command: str, timeout: int = 120) -> str:
        try:
            rc, out, err = sandbox.run_command(command, timeout)
        except subprocess.TimeoutExpired as e:
            raise ToolError(f"命令超时（>{timeout}s），已终止") from e
        out = (out or "").strip()
        err = (err or "").strip()
        if err:
            out = f"{out}\n[stderr]\n{err}" if out else f"[stderr]\n{err}"
        out = truncate(out or "(无输出)")
        if rc != 0:
            # 失败也带上完整输出：模型需要看到报错才能自愈
            raise ToolError(f"[退出码 {rc}]\n{out}")
        return out


def refresh_description() -> None:
    """按当前执行器更新 Bash 描述：docker 模式提示容器内路径约定，本地还原默认。"""
    if isinstance(sandbox.get_executor(), sandbox.DockerExecutor):
        BashTool.description = (
            "执行 shell 命令。命令在 Linux 容器内执行（sh），工作区挂载于 /workspace，"
            "无网络访问；Bash 内用 /workspace/... 路径，"
            "文件类工具（Read/Write/Edit/Grep/Glob）可直接使用 /workspace 前缀"
            "（自动映射到宿主工作区），也可用宿主绝对路径。")
    else:
        BashTool.description = DEFAULT_DESCRIPTION
