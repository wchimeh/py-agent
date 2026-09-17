# @File:     journal.py
# @Author:   mjh
# @DateTime: 2026/03/15/15:08
import json
import os
import re
import threading
from contextlib import suppress
from datetime import datetime, timedelta

_DATE_FILE = re.compile(r"^(\d{8})\.jsonl$")


class Journal:

    def __init__(self, path: str | None):
        self.path = path
        self._lock = threading.Lock()   # 多线程 append 同文件防交错行（P17 M2）
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def log(self, event: str, **fields) -> None:
        if not self.path:
            return
        rec = {"ts": datetime.now().isoformat(timespec="seconds"),
               "event": event, **fields}
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line)

    @classmethod
    def daily(cls, dir_: str = ".agent/logs", keep_days: int = 30) -> "Journal":
        os.makedirs(dir_, exist_ok=True)
        cls._rotate(dir_, keep_days)
        return cls(os.path.join(dir_, f"{datetime.now():%Y%m%d}.jsonl"))

    @staticmethod
    def _rotate(dir_: str, keep_days: int) -> None:
        """删除 keep_days 天前的日志文件；只动 YYYYMMDD.jsonl 命中的文件（keep_days=0 不清理）。"""
        if keep_days <= 0:
            return
        cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y%m%d")
        for name in os.listdir(dir_):
            m = _DATE_FILE.match(name)
            if m and m.group(1) < cutoff:
                with suppress(OSError):   # 清理失败不影响主流程
                    os.remove(os.path.join(dir_, name))







