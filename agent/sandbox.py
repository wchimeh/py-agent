# @File:     sandbox.py
# @DateTime: 2026/09/16
"""命令执行沙箱：Executor 抽象 + 全局装配（workspace.set_root 同款模式）。

LocalExecutor：宿主直接执行（原 bash.py 的 subprocess 语义平移）。
DockerExecutor：长驻受限容器 + 按命令 docker exec（M1 任务 1-4 实现）。
边界：沙箱只罩 Bash 工具；文件工具在宿主侧走 P7 路径硬限 + P4 权限门。
"""
import subprocess


class SandboxError(Exception):
    """沙箱容器创建/启动失败（镜像拉取失败、守护进程异常等）。"""


class LocalExecutor:

    def run(self, command: str, timeout: int) -> tuple[int, str, str]:
        """返回 (returncode, stdout, stderr) 原始值；超时抛 subprocess.TimeoutExpired。"""
        r = subprocess.run(command, shell=True, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        return r.returncode, r.stdout or "", r.stderr or ""


CONTAINER_WORKSPACE = "/workspace"   # 宿主工作区在容器内的固定挂载点


class DockerExecutor:
    """长驻受限容器 + 按命令 docker exec。懒启动：首次 run 才创建/复用容器。
    加固：无网络、内存/CPU/pids 上限、capability 全弃（仅回加 DAC_OVERRIDE 保容器 root
    对挂载工作区的常规读写）、禁止提权。"""

    def __init__(self, name: str, image: str, workspace_host: str,
                 memory: str = "2g", cpus: float = 2.0):
        self.name = name
        self.image = image
        self.workspace_host = workspace_host
        self.memory = memory
        self.cpus = cpus
        self._started = False

    def _docker(self, argv: list[str], timeout: int | None = None):
        return subprocess.run(["docker", *argv], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout)

    def _ensure_started(self) -> None:
        if self._started:
            return
        r = self._docker(["inspect", self.name])
        if r.returncode != 0:   # 同名容器不存在（含崩溃残留已清理）→ 创建
            r = self._docker(["run", "-d", "--name", self.name,
                              "--network", "none",
                              "--memory", self.memory,
                              "--cpus", str(self.cpus),
                              "--pids-limit", "256",
                              "--cap-drop", "ALL",
                              "--cap-add", "DAC_OVERRIDE",
                              "--security-opt", "no-new-privileges",
                              "-v", f"{self.workspace_host}:{CONTAINER_WORKSPACE}",
                              "-w", CONTAINER_WORKSPACE,
                              self.image, "sleep", "infinity"])
            if r.returncode != 0:
                raise SandboxError(
                    f"沙箱容器启动失败：{(r.stderr or '').strip()[:300]}\n"
                    f"          请确认 docker 正在运行且镜像可拉取（{self.image}）")
        self._started = True

    def run(self, command: str, timeout: int) -> tuple[int, str, str]:
        """容器内 sh 执行；返回 (returncode, stdout, stderr)；超时抛 TimeoutExpired。"""
        self._ensure_started()
        r = self._docker(["exec", "-w", CONTAINER_WORKSPACE,
                          self.name, "sh", "-c", command], timeout=timeout)
        return r.returncode, r.stdout or "", r.stderr or ""

    def stop(self) -> None:
        """会话结束清理容器；幂等（容器不在时 rm -f 也返回 0）。"""
        self._docker(["rm", "-f", self.name])
        self._started = False


_EXECUTOR = LocalExecutor()


def set_executor(executor) -> None:
    global _EXECUTOR
    _EXECUTOR = executor


def get_executor():
    return _EXECUTOR


def run_command(command: str, timeout: int) -> tuple[int, str, str]:
    """Bash 工具统一入口：委托当前执行器（格式化与 ToolError 语义在 bash.py）。"""
    return _EXECUTOR.run(command, timeout)


def probe_docker(timeout: int = 10) -> bool:
    """docker CLI/守护进程可用且为 Linux 容器引擎（Windows 引擎不支持 pids-limit 与 Linux 镜像）。"""
    r = None
    try:
        for argv in (["docker", "version"],
                     ["docker", "info", "--format", "{{.OSType}}"]):
            r = subprocess.run(argv, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout)
            if r.returncode != 0:
                return False
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.stdout.strip().lower() == "linux"
