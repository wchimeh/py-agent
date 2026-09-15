# -*- coding: utf-8 -*-
# @File:     test_context.py
# @Author:   mjh
# @DateTime: 2026/03/15
"""P5 上下文管理单测：token 估算、锚点校准、切分配对。"""
from agent.providers.base import (AssistantMessage, StopReason, ToolCall,
                                  ToolResultMessage, UserMessage, LLMResponse)
from agent.context import (ContextTracker, estimate_message_tokens,
                           estimate_tokens, render_for_summary,
                           split_for_compact)


def _resp(prompt=0, completion=0):
    return LLMResponse(text="ok", tool_calls=[], stop_reason=StopReason.END_TURN,
                       prompt_tokens=prompt, completion_tokens=completion)


# ---------- 字符估算 ----------

def test_estimate_tokens_ascii_cjk_and_empty():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1                 # 4 ASCII ≈ 1
    assert estimate_tokens("abc") == 1                  # 向上取整
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("中文") == 2                   # 非 ASCII 每字 1
    assert estimate_tokens("a中") == 2                   # 1 + ceil(1/4)

def test_estimate_grows_with_message_count():
    msgs = [UserMessage("hello world!"),
            AssistantMessage(text="好的，我来处理"),
            ToolResultMessage("t1", "x" * 40)]
    running = 0
    for m in msgs:
        running += estimate_message_tokens(m)
        assert ContextTracker().estimate(msgs[:msgs.index(m) + 1]) >= running - 1


# ---------- 锚点校准 ----------

def test_tracker_anchor_plus_increment():
    msgs = [UserMessage("问题"), AssistantMessage(text="回答")]
    t = ContextTracker()
    t.update_anchor(_resp(prompt=1000), msgs)
    assert t.anchor_len == 2 and t.anchor_tokens == 1000
    extra = AssistantMessage(text="追加说明")
    msgs.append(extra)
    est = t.estimate(msgs)
    assert est == 1000 + estimate_message_tokens(extra)   # 真实锚点 + 增量
    assert est > 1000

def test_tracker_reset_falls_back_to_pure_estimate():
    msgs = [UserMessage("abc")]
    t = ContextTracker()
    t.update_anchor(_resp(prompt=500), msgs)
    t.reset()
    assert t.estimate(msgs) == estimate_message_tokens(msgs[0])  # 无锚点纯估算

def test_anchor_kept_when_streaming_usage_missing():
    # 流式服务端不回 usage（计 0）时保持旧锚点，估算继续可用
    msgs = [UserMessage("问题"), AssistantMessage(text="回答")]
    t = ContextTracker()
    t.update_anchor(_resp(prompt=1000), msgs)
    t.update_anchor(_resp(prompt=0), msgs)     # 不重锚
    assert t.anchor_tokens == 1000 and t.anchor_len == 2
    extra = UserMessage("追问")
    msgs.append(extra)
    assert t.estimate(msgs) == 1000 + estimate_message_tokens(extra)


# ---------- 切分与配对 ----------

def build_history():
    tc1 = ToolCall(id="t1", name="Write", arguments={"file_path": "a", "content": "1"})
    tc2 = ToolCall(id="t2", name="Bash", arguments={"command": "dir"})
    return [
        UserMessage("第一个任务"),
        AssistantMessage(tool_calls=[tc1]),
        ToolResultMessage("t1", "ok"),
        AssistantMessage(tool_calls=[tc2]),
        ToolResultMessage("t2", "ok"),
        UserMessage("第二个任务"),
        AssistantMessage(text="完成"),
    ]  # 7 条：u a r a r u a


def assert_pairing(kept):
    uses, results = set(), set()
    for m in kept:
        if isinstance(m, AssistantMessage):
            uses.update(tc.id for tc in m.tool_calls)
        elif isinstance(m, ToolResultMessage):
            results.add(m.tool_call_id)
    assert uses == results, f"kept 内配对破坏: use={uses} result={results}"


def test_split_plain_cut_on_assistant():
    old, kept = split_for_compact(build_history(), keep_recent=4)
    # cut = 7-4 = 3，messages[3] 是 AssistantMessage，无需前移
    assert len(old) == 3 and len(kept) == 4
    assert_pairing(kept)

def test_split_moves_cut_back_off_orphan_tool_result():
    old, kept = split_for_compact(build_history(), keep_recent=5)
    # cut = 7-5 = 2，messages[2] 是 ToolResultMessage → 前移到 1
    # 保配对优先于严格条数：kept 6 条 >= keep_recent
    assert len(old) == 1 and len(kept) == 6
    assert not isinstance(kept[0], ToolResultMessage)
    assert_pairing(kept)

def test_split_consecutive_tool_results_step_back_over_group():
    tc1 = ToolCall(id="t1", name="Write", arguments={})
    tc2 = ToolCall(id="t2", name="Read", arguments={})
    tc3 = ToolCall(id="t3", name="Bash", arguments={})
    msgs = [
        UserMessage("任务"),
        AssistantMessage(tool_calls=[tc1, tc2]),   # 一轮双工具
        ToolResultMessage("t1", "ok"),
        ToolResultMessage("t2", "ok"),
        AssistantMessage(text="继续"),
        AssistantMessage(tool_calls=[tc3]),
        ToolResultMessage("t3", "ok"),
        AssistantMessage(text="完成"),
    ]  # 8 条
    old, kept = split_for_compact(msgs, keep_recent=5)
    # cut = 3 → messages[3] 是 R(t2) → 前移 2 → messages[1] 是 A(双工具) 停
    assert len(old) == 1
    assert isinstance(kept[0], AssistantMessage) and len(kept[0].tool_calls) == 2
    assert_pairing(kept)

def test_split_short_history_compacts_only_first():
    msgs = [UserMessage("hi"), AssistantMessage(text="ok")]
    old, kept = split_for_compact(msgs, keep_recent=8)
    # cut = max(1, 2-8) = 1：至少压缩 1 条，不崩
    assert len(old) == 1 and len(kept) == 1


# ---------- 摘要渲染 ----------

def test_render_for_summary_marks_roles():
    text = render_for_summary(build_history())
    assert "[用户] 第一个任务" in text
    assert "[助手调用工具] Write" in text
    assert "[助手调用工具] Bash" in text
    assert "[工具结果] ok" in text
    assert "第二个任务" in text

def test_render_for_summary_error_tag_and_truncation():
    r = ToolResultMessage("t", "x" * 500, is_error=True)
    text = render_for_summary([r])
    assert "工具结果(错误)" in text
    assert "x" * 300 in text and "x" * 400 not in text   # 截到 300 字符
