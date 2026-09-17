# @File:     viewer.py
# @DateTime: 2026/09/17
"""子代理输出查看器（P17 M2）：SubagentRegistry + 渲染纯函数 + Ctrl+T 键监听。
渲染函数纯化（snapshot→菜单串 / transcript→文本串）便于单测；
键监听仅任务执行期间由 main 挂载（与 prompt_toolkit 终端所有权互斥）。"""
import sys
import threading
from collections import deque
from contextlib import suppress
from datetime import datetime

from .tools.subagent import CONSOLE_LOCK

CTRL_T = "\x14"


class SubagentRegistry:
    """子代理登记簿：id/描述/状态/起止时间 + 有界 transcript 行缓冲（线程安全）。"""

    def __init__(self, maxlen: int = 2000):
        self._lock = threading.Lock()
        self._records: list[dict] = []
        self._maxlen = maxlen

    def register(self, agent_id: str, description: str) -> None:
        with self._lock:
            self._records.append({"id": agent_id, "description": description,
                                  "status": "running", "started": datetime.now(),
                                  "ended": None, "buf": deque(maxlen=self._maxlen)})

    def add_lines(self, agent_id: str, *lines: str) -> None:
        with self._lock:
            for r in self._records:
                if r["id"] == agent_id:
                    r["buf"].extend(lines)
                    return

    def update(self, agent_id: str, **fields) -> None:
        with self._lock:
            for r in self._records:
                if r["id"] == agent_id:
                    r.update(fields)
                    return

    def snapshot(self) -> list[dict]:
        """浅拷贝（不含 buf），渲染用。"""
        with self._lock:
            return [{"id": r["id"], "description": r["description"],
                     "status": r["status"], "started": r["started"],
                     "ended": r["ended"], "lines": len(r["buf"])}
                    for r in self._records]

    def get_lines(self, agent_id: str) -> list[str]:
        with self._lock:
            for r in self._records:
                if r["id"] == agent_id:
                    return list(r["buf"])
            return []


def render_menu(records: list[dict]) -> str:
    if not records:
        return "（暂无子代理记录）"
    head = "子代理列表（输入编号查看 · 其他键返回 · 查看期间子代理照跑）"
    rows = [f"  {i}) {r['id']} · {r['description'][:30]} · {r['status']} · {r['lines']} 行"
            for i, r in enumerate(records, 1)]
    return "\n".join([head, *rows])


def render_transcript(lines: list[str], title: str, tail: int = 40) -> str:
    shown = list(lines)[-tail:]
    head = f"── {title}（尾部 {len(shown)}/{len(lines)} 行，任意键返回）──"
    return "\n".join([head, *shown])


def browse(registry: SubagentRegistry, selection: int | None = None) -> str:
    """/agents 入口：无编号=菜单；有编号=该子代理 transcript 尾部。"""
    snap = registry.snapshot()
    if not snap:
        return "（暂无子代理记录）"
    if selection is None:
        return render_menu(snap)
    if not 1 <= selection <= len(snap):
        return f"编号超出范围（1~{len(snap)}）"
    r = snap[selection - 1]
    return render_transcript(registry.get_lines(r["id"]),
                             title=f"{r['id']} {r['description']}")


class KeyListener:
    """Ctrl+T 查看器监听：仅任务执行期间挂载（start/stop 由 main 控制）。
    Windows 走 msvcrt 轮询；POSIX 走 termios cbreak + select。
    挂载失败（无 tty）不阻塞任务：start() 返回 False，main 降级提示 /agents。"""

    def __init__(self, registry: SubagentRegistry):
        self.registry = registry
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._term = None       # POSIX 恢复用原始终端属性

    def start(self) -> bool:
        try:
            self._term = self._mount()
        except Exception:
            return False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        self._unmount()

    # ---- 平台挂载 ----

    def _mount(self):
        if sys.platform == "win32":
            import msvcrt  # noqa: F401  导入成功即可用（kbhit/getch 轮询无需改终端模式）
            return None
        import termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[3] &= ~(termios.ECHO | termios.ICANON)   # cbreak：关回显与行缓冲
        termios.tcsetattr(fd, termios.TCSANOW, new)
        return old

    def _unmount(self) -> None:
        if self._term is None:
            return
        try:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, self._term)
        except Exception:
            pass
        self._term = None

    def _read_key(self, timeout: float = 0.1) -> str | None:
        """等待一个按键；超时返回 None（供 stop 检查）。Windows msvcrt / POSIX select。"""
        if sys.platform == "win32":
            import msvcrt
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):     # 方向键等前缀：吞掉后续码
                    msvcrt.getwch()
                    return None
                return ch
            self._stop.wait(0.05)
            return None
        import select
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if r:
            return sys.stdin.read(1)
        return None

    # ---- 主循环与交互 ----

    def _run(self) -> None:
        while not self._stop.is_set():
            key = self._read_key()
            if key == CTRL_T:
                with suppress(Exception):
                    self._interact()   # 查看器异常不影响任务

    def _interact(self) -> None:
        snap = self.registry.snapshot()
        with CONSOLE_LOCK:
            print("\n" + render_menu(snap))
        if not snap:
            self._wait_any_key()
            return
        while not self._stop.is_set():
            key = self._read_key(timeout=0.5)
            if key is None:
                continue
            if not (key and key.isdigit() and 1 <= int(key) <= len(snap)):
                return                          # 非数字=返回任务画面
            r = snap[int(key) - 1]
            with CONSOLE_LOCK:
                print("\n" + render_transcript(self.registry.get_lines(r["id"]),
                                               title=f"{r['id']} {r['description']}"))
            self._wait_any_key()
            return

    def _wait_any_key(self) -> None:
        while not self._stop.is_set():
            if self._read_key(timeout=0.2) is not None:
                return
