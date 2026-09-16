# @File:     spinner.py
# @Author:   mjh
# @DateTime: 2026/03/14/16:08
import itertools
import sys
import threading
import time


class Spinner:
    BRAILLE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"   # 盲文点阵帧，Win Terminal 正常
    ASCII = "|/-\\"                  # 老式 conhost 字体不支持盲文时换这组

    def __init__(self, text: str = "思考中", frames: str = BRAILLE):
        self.text = text
        self.frames = frames
        self._stop = threading.Event()
        self._t0 = time.monotonic()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._running = False

    def _spin(self):
        for ch in itertools.cycle(self.frames):
            # Event.wait 兼当 sleep：既能控帧率，又能被立刻唤醒退出
            if self._stop.wait(0.1):
              return
            secs = time.monotonic() - self._t0
            sys.stdout.write(f"\r{ch} {self.text} {secs:4.1f}s")
            sys.stdout.flush()

    def __enter__(self):
        self._thread.start()
        self._running = True
        return self

    def stop(self):
        """停止并清理动画行；幂等（重复调用、未启动时均安全）。"""
        if not self._running:
            return
        self._running = False
        self._stop.set()
        self._thread.join()
        # 用空格覆盖整行再回到行首，清干净动画残留
        sys.stdout.write("\r" + " " * (len(self.text) + 12) + "\r")
        sys.stdout.flush()

    def __exit__(self, exc_type, exc, tb):
        self.stop()

