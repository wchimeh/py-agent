# @File:     budget.py
# @DateTime: 2026/09/17
"""进程级 token 预算池：主循环与全部子循环共享的总闸（线程安全）。
与 AgentLoop.token_budget（单任务硬限）并存：池尽即全体终止。"""
import threading


class BudgetPool:

    def __init__(self, total: int):
        self.total = total
        self._used = 0
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return self.total - self._used

    def consume(self, n: int) -> bool:
        """成功扣减返回 True；欠足拒付（不部分扣减）返回 False。"""
        with self._lock:
            if self._used + n > self.total:
                return False
            self._used += n
            return True
