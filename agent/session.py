# -*- coding: utf-8 -*-
# @File:     session.py
# @Author:   mjh
# @DateTime: 2026/03/15/15:05
"""会话持久化：多会话存储（一进程一会话一文件，按会话 ID 索引）。"""
import json
import os
import secrets
from datetime import datetime

from .providers.base import AssistantMessage, Message, ToolCall, ToolResultMessage, UserMessage

SESSIONS_DIR = ".agent/sessions"


class SessionError(Exception):
    """
    会话文件损坏或内容不识别。
    """


def new_session_id() -> str:
    """YYYYMMDD_HHMMSS_xxxx：时间前缀（文件名字典序=时间序）+ 随机防同秒碰撞。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(2)


def dump_message(m: Message) -> dict:
    if isinstance(m, AssistantMessage):
        return {"role": "assistant", "text": m.text,
                "tool_calls": [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in m.tool_calls]}
    if isinstance(m, ToolResultMessage):
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content, "is_error": m.is_error}
    return {"role": "user", "content": m.content}


def load_message(d: dict) -> Message:
    try:
        role = d["role"]
        if role == "user":
            return UserMessage(content=d["content"])
        if role == "assistant":
            return AssistantMessage(
                text=d.get("text"),
                tool_calls=[ToolCall(id=t["id"], name=t["name"], arguments=t["arguments"]) for t in d.get("tool_calls", [])])
        if role == "tool":
            return ToolResultMessage(tool_call_id=d["tool_call_id"], content=d["content"], is_error=d.get("is_error", False))
        raise SessionError(f"未知消息 role: {role}")
    except SessionError:
        raise
    except KeyError as e:
        raise SessionError(f"消息缺字段: {e}") from e


def save_session(sid: str, messages: list[Message], dir_: str = SESSIONS_DIR) -> None:
    os.makedirs(dir_, exist_ok=True)
    first = next((m.content for m in messages if isinstance(m, UserMessage)), "")
    data = {"id": sid,
            "created": datetime.now().isoformat(timespec="seconds"),
            "first_input": first[:60],
            "messages": [dump_message(m) for m in messages]}
    path = os.path.join(dir_, f"{sid}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)  # Windows 下原子覆盖，防崩溃半写


def load_session(sid: str, dir_: str = SESSIONS_DIR) -> list[Message]:
    path = os.path.join(dir_, f"{sid}.json")
    with open(path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise SessionError(f"JSON 解析失败: {e}") from e
    if not isinstance(data, dict) or "messages" not in data:
        raise SessionError("会话文件缺 messages 字段")
    return [load_message(d) for d in data["messages"]]  # 文件不存在时自然抛 FileNotFoundError


def list_sessions(dir_: str = SESSIONS_DIR) -> list[dict]:
    """列出全部会话（最新在前）。单文件损坏不炸列表，标 corrupt=True。"""
    if not os.path.isdir(dir_):
        return []
    out = []
    for name in os.listdir(dir_):
        if not name.endswith(".json"):
            continue
        rec = {"id": name[:-5], "created": "", "first_input": "",
               "msgs": 0, "corrupt": False}
        try:
            with open(os.path.join(dir_, name), encoding="utf-8") as f:
                data = json.load(f)
            rec.update(created=data.get("created", ""),
                       first_input=data.get("first_input", ""),
                       msgs=len(data.get("messages", [])))
        except (OSError, json.JSONDecodeError):
            rec["corrupt"] = True
        out.append(rec)
    return sorted(out, key=lambda r: r["id"], reverse=True)
