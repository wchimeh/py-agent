# -*- coding: utf-8 -*-
# @File:     test_session.py
# @Author:   mjh
# @DateTime: 2026/03/15
"""多会话持久化测试：ID 生成、按 sid 存取、元数据、列表与损坏标注。"""
import json
import os
import re

import pytest

from agent.providers.base import AssistantMessage, ToolCall, ToolResultMessage, UserMessage
from agent.session import (SessionError, list_sessions, load_message,
                           load_session, new_session_id, save_session)


MSGS = [
    UserMessage("写文件"),
    AssistantMessage(text="好的", tool_calls=[
        ToolCall(id="t1", name="Write", arguments={"file_path": "a", "content": "x"})]),
    ToolResultMessage("t1", "写入 3 行", is_error=True),
    AssistantMessage(text="完成"),                       # 空 tool_calls 的往返
]


# ---------- 会话 ID ----------

def test_new_session_id_format_and_unique():
    ids = {new_session_id() for _ in range(20)}
    assert len(ids) == 20                                # 无碰撞
    for sid in ids:
        assert re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{4}", sid)


# ---------- 按 sid 存取 ----------

def test_roundtrip_preserves_all_fields(tmp_path):
    d = str(tmp_path)
    save_session("s1", MSGS, dir_=d)
    assert load_session("s1", dir_=d) == MSGS            # dataclass 逐字段相等


def test_two_sessions_no_overwrite(tmp_path):
    d = str(tmp_path)
    save_session("s1", MSGS, dir_=d)
    save_session("s2", MSGS[:1], dir_=d)
    assert load_session("s1", dir_=d) == MSGS            # 互不覆盖
    assert load_session("s2", dir_=d) == MSGS[:1]


def test_save_creates_dir_and_overwrites(tmp_path):
    d = str(tmp_path / "nested")
    save_session("s1", MSGS, dir_=d)                     # 自动建目录
    save_session("s1", MSGS[:1], dir_=d)                 # 同 sid 二次保存为覆盖
    assert load_session("s1", dir_=d) == MSGS[:1]
    assert not os.path.exists(os.path.join(d, "s1.json.tmp"))   # 临时文件不残留


def test_first_input_metadata(tmp_path):
    d = str(tmp_path)
    save_session("s1", MSGS, dir_=d)
    data = json.load(open(os.path.join(d, "s1.json"), encoding="utf-8"))
    assert data["id"] == "s1"
    assert data["first_input"] == "写文件"
    assert len(data["messages"]) == 4
    # 空消息与会话摘要开头：取首条 UserMessage、截断 60
    save_session("s2", [], dir_=d)
    assert json.load(open(os.path.join(d, "s2.json"), encoding="utf-8"))["first_input"] == ""
    save_session("s3", [UserMessage("长" * 100)], dir_=d)
    assert len(json.load(open(os.path.join(d, "s3.json"), encoding="utf-8"))["first_input"]) == 60


# ---------- 损坏与缺失 ----------

def test_load_missing_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_session("nope", dir_=str(tmp_path))


def test_corrupt_json_raises_sessionerror(tmp_path):
    (tmp_path / "s1.json").write_text("{不是json", encoding="utf-8")
    with pytest.raises(SessionError):
        load_session("s1", dir_=str(tmp_path))


def test_legacy_array_format_raises(tmp_path):
    # 旧版纯数组格式（无 messages 字段）不可加载
    (tmp_path / "s1.json").write_text("[]", encoding="utf-8")
    with pytest.raises(SessionError):
        load_session("s1", dir_=str(tmp_path))


def test_unknown_role_raises():
    with pytest.raises(SessionError):
        load_message({"role": "alien", "content": "x"})


def test_missing_field_raises():
    with pytest.raises(SessionError):
        load_message({"role": "tool", "content": "x"})   # 缺 tool_call_id


# ---------- list_sessions ----------

def test_list_sessions_desc_and_fields(tmp_path):
    d = str(tmp_path)
    save_session("20260914_090000_aa01", MSGS, dir_=d)
    save_session("20260915_100000_bb02", MSGS[:1], dir_=d)
    save_session("20260915_100005_cc03", MSGS, dir_=d)
    sessions = list_sessions(dir_=d)
    assert [s["id"] for s in sessions] == \
        ["20260915_100005_cc03", "20260915_100000_bb02", "20260914_090000_aa01"]  # 最新在前
    assert sessions[0]["msgs"] == 4 and sessions[0]["first_input"] == "写文件"
    assert sessions[1]["msgs"] == 1 and sessions[2]["msgs"] == 4


def test_list_sessions_marks_corrupt_not_crash(tmp_path):
    d = str(tmp_path)
    save_session("20260915_100000_aa01", MSGS, dir_=d)
    (tmp_path / "broken.json").write_text("{坏", encoding="utf-8")
    (tmp_path / "readme.txt").write_text("忽略非 json", encoding="utf-8")
    sessions = list_sessions(dir_=d)
    assert len(sessions) == 2                            # 损坏文件占位但不炸列表
    by_id = {s["id"]: s for s in sessions}
    assert by_id["broken"]["corrupt"] is True
    assert by_id["20260915_100000_aa01"]["corrupt"] is False


def test_list_sessions_missing_dir_returns_empty(tmp_path):
    assert list_sessions(dir_=str(tmp_path / "nope")) == []
