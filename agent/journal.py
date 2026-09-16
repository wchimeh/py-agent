# @File:     journal.py
# @Author:   mjh
# @DateTime: 2026/03/15/15:08
import json
import os
from datetime import datetime


class Journal:

    def __init__(self, path: str | None):
        self.path = path
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def log(self, event: str, **fields) -> None:
        if not self.path:
            return
        rec = {"ts": datetime.now().isoformat(timespec="seconds"),
               "event": event, **fields}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    @classmethod
    def daily(cls, dir_: str = ".agent/logs") -> "Journal":
        os.makedirs(dir_, exist_ok=True)
        return cls(os.path.join(dir_, f"{datetime.now():%Y%m%d}.jsonl"))







