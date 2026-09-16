# @File:     test_providers.py
# @Author:   mjh
# @DateTime: 2026/03/15
"""双协议转换层单测：_convert 纯函数直测 + FakeClient 属性替换，零网络。"""
import json
from types import SimpleNamespace

import pytest

from agent.providers.anthropic_provider import AnthropicProvider
from agent.providers.base import (
    MALFORMED_ARGS_KEY,
    AssistantMessage,
    LLMProvider,
    LLMResponse,
    StopReason,
    ToolCall,
    ToolDef,
    ToolResultMessage,
    UserMessage,
)
from agent.providers.openai_provider import OpenAIProvider, _ThinkFilter


def make_openai():
    # SDK 构造不发网络请求；chat 前会把 client 替换为假对象
    return OpenAIProvider(model="m", api_key="k", base_url="http://localhost:1")


def make_anthropic():
    return AnthropicProvider(model="m", api_key="k", base_url="http://localhost:1")


MSGS = [
    UserMessage("写文件"),
    AssistantMessage(tool_calls=[ToolCall(id="t1", name="Write",
                                          arguments={"file_path": "a", "content": "x"})]),
    ToolResultMessage("t1", "ok"),
    AssistantMessage(text="完成"),
]


# ---------- OpenAI：_convert ----------

def test_openai_convert_shapes():
    out = make_openai()._convert(MSGS, system="SYS")
    assert out[0] == {"role": "system", "content": "SYS"}
    assert out[1] == {"role": "user", "content": "写文件"}
    tc = out[2]["tool_calls"][0]
    assert tc["id"] == "t1" and tc["type"] == "function"
    assert tc["function"]["name"] == "Write"
    # OpenAI 的 arguments 是 JSON 字符串
    assert json.loads(tc["function"]["arguments"]) == {"file_path": "a", "content": "x"}
    assert out[3] == {"role": "tool", "tool_call_id": "t1", "content": "ok"}
    # 纯文本 assistant 不带 tool_calls 键
    assert out[4] == {"role": "assistant", "content": "完成"}

def test_openai_convert_without_system():
    out = make_openai()._convert([UserMessage("hi")], system="")
    assert out == [{"role": "user", "content": "hi"}]


# ---------- OpenAI：chat 端到端（FakeClient） ----------

class FakeOpenAIClient:
    def __init__(self, resp):
        self.kwargs = None
        outer = self

        class Completions:
            def create(self, **kwargs):
                outer.kwargs = kwargs
                return resp
        self.chat = SimpleNamespace(completions=Completions())


def test_openai_chat_parses_response():
    p = make_openai()
    inner_call = SimpleNamespace(
        id="t9", function=SimpleNamespace(name="Bash",
                                          arguments='{"command": "dir"}'))
    fake = FakeOpenAIClient(SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="tool_calls",
                                 message=SimpleNamespace(content="先写文件",
                                                         tool_calls=[inner_call]))],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20)))
    p.client = fake
    resp = p.chat([UserMessage("x")], system="S")

    assert resp.text == "先写文件"
    assert resp.stop_reason is StopReason.TOOL_USE
    assert resp.prompt_tokens == 100 and resp.completion_tokens == 20
    assert resp.tool_calls[0].arguments == {"command": "dir"}   # JSON 字符串 → dict
    # 请求形状：system 首位、无 tools 时不带键
    assert fake.kwargs["model"] == "m"
    assert fake.kwargs["messages"][0]["role"] == "system"
    assert "tools" not in fake.kwargs

def test_openai_chat_missing_usage_counts_zero():
    p = make_openai()
    fake = FakeOpenAIClient(SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop",
                                 message=SimpleNamespace(content="hi", tool_calls=None))],
        usage=None))  # 服务端不返回 usage：不得抛 AttributeError（问题 7）
    p.client = fake
    resp = p.chat([UserMessage("x")])
    assert resp.prompt_tokens == 0 and resp.completion_tokens == 0


def test_openai_chat_tools_schema_and_stop_map():
    p = make_openai()
    fake = FakeOpenAIClient(SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="length",
                                 message=SimpleNamespace(content="截断", tool_calls=None))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1)))
    p.client = fake
    resp = p.chat([UserMessage("x")],
                  tools=[ToolDef(name="Read", description="读文件",
                                 parameters={"type": "object"})], system="")
    assert resp.stop_reason is StopReason.MAX_TOKENS and resp.tool_calls == []
    spec = fake.kwargs["tools"][0]
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "Read"
    assert spec["function"]["parameters"] == {"type": "object"}


# ---------- Anthropic：_convert ----------

def test_anthropic_convert_blocks():
    out = make_anthropic()._convert(MSGS)
    assert out[0] == {"role": "user", "content": "写文件"}
    # assistant 的 tool_use 是 content block，input 是 dict
    assert out[1]["content"] == [{"type": "tool_use", "id": "t1", "name": "Write",
                                  "input": {"file_path": "a", "content": "x"}}]
    # tool_result 变成 user 消息的 content block
    assert out[2]["role"] == "user"
    assert out[2]["content"][0] == {"type": "tool_result", "tool_use_id": "t1",
                                    "content": "ok"}
    assert out[3] == {"role": "assistant", "content": [{"type": "text", "text": "完成"}]}

def test_anthropic_text_plus_tool_use_blocks():
    a = AssistantMessage(text="说明", tool_calls=[
        ToolCall(id="t1", name="Bash", arguments={"command": "dir"})])
    out = make_anthropic()._convert([UserMessage("q"), a])
    assert out[1]["content"] == [
        {"type": "text", "text": "说明"},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "dir"}}]

def test_anthropic_consecutive_tool_results_merge_into_one_user():
    a = AssistantMessage(tool_calls=[
        ToolCall(id="t1", name="Write", arguments={}),
        ToolCall(id="t2", name="Read", arguments={})])
    msgs = [UserMessage("go"), a,
            ToolResultMessage("t1", "r1"),
            ToolResultMessage("t2", "r2", is_error=True)]
    out = make_anthropic()._convert(msgs)
    assert len(out) == 3                       # u / assistant / 合并后的 user
    blocks = out[2]["content"]
    assert len(blocks) == 2                    # 同批结果必须合并
    assert blocks[0] == {"type": "tool_result", "tool_use_id": "t1", "content": "r1"}
    assert blocks[1]["tool_use_id"] == "t2" and blocks[1]["is_error"] is True


# ---------- Anthropic：chat 端到端（FakeClient） ----------

class FakeAnthropicClient:
    def __init__(self, resp):
        self.kwargs = None
        outer = self

        class Messages:
            def create(self, **kwargs):
                outer.kwargs = kwargs
                return resp
        self.messages = Messages()


def test_anthropic_chat_parses_response():
    p = make_anthropic()
    fake = FakeAnthropicClient(SimpleNamespace(
        content=[SimpleNamespace(type="text", text="部分一"),
                 SimpleNamespace(type="text", text="部分二"),
                 SimpleNamespace(type="tool_use", id="t9", name="Bash",
                                 input={"command": "dir"})],
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=50, output_tokens=10)))
    p.client = fake
    resp = p.chat([UserMessage("x")], system="SYS")

    assert resp.text == "部分一\n部分二"        # 多 text block 拼接
    assert resp.tool_calls[0].arguments == {"command": "dir"}
    assert resp.stop_reason is StopReason.TOOL_USE
    assert resp.prompt_tokens == 50 and resp.completion_tokens == 10
    # 请求形状：system 是顶层参数、无 tools 时不带键
    assert fake.kwargs["system"] == "SYS"
    assert "tools" not in fake.kwargs
    assert fake.kwargs["messages"] == [{"role": "user", "content": "x"}]

def test_anthropic_chat_tools_schema_and_no_system():
    p = make_anthropic()
    fake = FakeAnthropicClient(SimpleNamespace(
        content=[], stop_reason="max_tokens",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1)))
    p.client = fake
    resp = p.chat([UserMessage("x")],
                  tools=[ToolDef(name="Read", description="d",
                                 parameters={"type": "object"})], system="")
    assert resp.stop_reason is StopReason.MAX_TOKENS
    assert resp.text is None                   # 无 text block 时为 None
    assert "system" not in fake.kwargs         # 空 system 不设键
    assert fake.kwargs["tools"][0] == {
        "name": "Read", "description": "d", "input_schema": {"type": "object"}}


# ---------- P8：chat_stream 流式 ----------

def test_default_chat_stream_delegates_to_chat():
    class Minimal(LLMProvider):
        def __init__(self):
            self.calls = 0

        def chat(self, messages, tools=None, system=""):
            self.calls += 1
            return LLMResponse(text="x", tool_calls=[], stop_reason=StopReason.END_TURN)

    m = Minimal()
    resp = m.chat_stream([], on_text=lambda t: None)
    assert m.calls == 1 and resp.text == "x"   # 基类默认实现走非流式


class _StreamHandle:
    """模拟 SDK 的 messages.stream() 返回：上下文管理器 + text_stream + final"""

    def __init__(self, owner):
        self._owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        return iter(self._owner.deltas)

    def get_final_message(self):
        return self._owner.final


class FakeAnthropicStreamClient:
    def __init__(self, deltas, final):
        self.deltas, self.final = deltas, final
        self.kwargs = None
        outer = self

        class Messages:
            def stream(self, **kwargs):
                outer.kwargs = kwargs
                return _StreamHandle(outer)
        self.messages = Messages()


def test_anthropic_chat_stream_deltas_and_final():
    p = make_anthropic()
    fake = FakeAnthropicStreamClient(
        ["你", "好"],
        SimpleNamespace(
            content=[SimpleNamespace(type="text", text="你好"),
                     SimpleNamespace(type="tool_use", id="t9", name="Bash",
                                     input={"command": "dir"})],
            stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=50, output_tokens=10)))
    p.client = fake
    seen = []
    resp = p.chat_stream([UserMessage("x")], system="SYS", on_text=seen.append)

    assert seen == ["你", "好"]                # 增量按序回调
    assert resp.text == "你好" and resp.prompt_tokens == 50
    assert resp.tool_calls[0].arguments == {"command": "dir"}
    assert resp.stop_reason is StopReason.TOOL_USE
    assert fake.kwargs["system"] == "SYS"      # 请求形状与 chat 一致

def test_anthropic_chat_stream_none_on_text_delegates_to_chat():
    p = make_anthropic()
    fake = FakeAnthropicClient(SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hi")],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1)))
    p.client = fake
    resp = p.chat_stream([UserMessage("x")])   # 无回调 → 非流式
    assert resp.text == "hi" and fake.kwargs is not None


class FakeOpenAIStreamClient:
    def __init__(self, chunks):
        self.chunks, self.kwargs = chunks, None
        outer = self

        class Completions:
            def create(self, **kwargs):
                outer.kwargs = kwargs
                return iter(outer.chunks)
        self.chat = SimpleNamespace(completions=Completions())


def _oai_chunk(content=None, tool_calls=None, finish=None, usage=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta,
                                                    finish_reason=finish)],
                           usage=usage)


def test_openai_chat_stream_text_accumulates():
    p = make_openai()
    p.client = FakeOpenAIStreamClient([
        _oai_chunk(content="你"),
        _oai_chunk(content="好"),
        _oai_chunk(finish="stop"),
        SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=7,
                                                          completion_tokens=3)),
    ])
    seen = []
    resp = p.chat_stream([UserMessage("x")], on_text=seen.append)

    assert seen == ["你", "好"] and resp.text == "你好"
    assert resp.stop_reason is StopReason.END_TURN
    assert resp.prompt_tokens == 7 and resp.completion_tokens == 3
    assert p.client.kwargs["stream"] is True
    assert p.client.kwargs["stream_options"] == {"include_usage": True}

def test_openai_chat_stream_tool_args_concat_across_chunks():
    p = make_openai()
    def tc(id=None, name=None, args=None):
        return SimpleNamespace(
            index=0, id=id,
            function=SimpleNamespace(name=name, arguments=args))
    p.client = FakeOpenAIStreamClient([
        _oai_chunk(tool_calls=[tc(id="t1", name="Write", args='{"file_p')]),
        _oai_chunk(tool_calls=[tc(args='ath": "a.txt", "content": "x"}')]),
        _oai_chunk(finish="tool_calls",
                   usage=SimpleNamespace(prompt_tokens=9, completion_tokens=4)),
    ])
    resp = p.chat_stream([UserMessage("x")], on_text=lambda t: None)

    assert resp.stop_reason is StopReason.TOOL_USE
    assert resp.tool_calls[0].id == "t1"
    assert resp.tool_calls[0].arguments == {"file_path": "a.txt", "content": "x"}

def test_openai_chat_stream_missing_usage_counts_zero():
    p = make_openai()
    p.client = FakeOpenAIStreamClient([
        _oai_chunk(content="hi"),
        _oai_chunk(finish="stop", usage=None),
    ])
    resp = p.chat_stream([UserMessage("x")], on_text=lambda t: None)
    assert resp.prompt_tokens == 0 and resp.completion_tokens == 0


# ---------- OpenAI：非法 JSON 兜底（P9：不击穿循环，哨兵回灌） ----------

def test_openai_chat_malformed_json_arguments_not_raise():
    p = make_openai()
    inner_call = SimpleNamespace(
        id="t9", function=SimpleNamespace(name="Bash",
                                          arguments='{"command": '))  # 截断的非法 JSON
    fake = FakeOpenAIClient(SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="tool_calls",
                                 message=SimpleNamespace(content=None,
                                                         tool_calls=[inner_call]))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1)))
    p.client = fake
    resp = p.chat([UserMessage("x")])

    assert resp.tool_calls[0].name == "Bash"
    assert resp.tool_calls[0].arguments[MALFORMED_ARGS_KEY] == '{"command": '

def test_openai_chat_stream_malformed_json_arguments_not_raise():
    p = make_openai()
    def tc(id=None, name=None, args=None):
        return SimpleNamespace(
            index=0, id=id,
            function=SimpleNamespace(name=name, arguments=args))
    p.client = FakeOpenAIStreamClient([
        _oai_chunk(tool_calls=[tc(id="t1", name="Write", args='{"file_p')]),
        _oai_chunk(tool_calls=[tc(args='ath": ')]),  # 拼完仍非法
        _oai_chunk(finish="tool_calls",
                   usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1)),
    ])
    resp = p.chat_stream([UserMessage("x")], on_text=lambda t: None)

    assert resp.tool_calls[0].name == "Write"
    assert MALFORMED_ARGS_KEY in resp.tool_calls[0].arguments

def test_openai_chat_stream_badrequest_falls_back_to_chat():
    import httpx
    from openai import BadRequestError
    plain = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop",
                                 message=SimpleNamespace(content="降级回答",
                                                         tool_calls=None))],
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2))

    class FallbackClient:
        def __init__(self):
            self.calls = []
            outer = self

            class Completions:
                def create(self, **kwargs):
                    outer.calls.append(sorted(kwargs))
                    if "stream_options" in kwargs:
                        raise BadRequestError(
                            "unsupported stream_options",
                            response=httpx.Response(400, request=httpx.Request(
                                "POST", "http://localhost:1")), body=None)
                    return plain
            self.chat = SimpleNamespace(completions=Completions())

    p = make_openai()
    p.client = FallbackClient()
    seen = []
    resp = p.chat_stream([UserMessage("x")], on_text=seen.append)

    assert resp.text == "降级回答" and resp.prompt_tokens == 5
    assert seen == []                          # 非流式降级不产生增量
    assert len(p.client.calls) == 2            # 先流式 400，再非流式成功


# ---------- P15：think 标签剥离（openai 协议推理模型思考段） ----------


def test_think_filter_single_chunk():
    f = _ThinkFilter()
    assert f.feed("<think>思考</think>你好") == "你好"


def test_think_filter_tag_split_across_chunks():
    f = _ThinkFilter()
    assert f.feed("<th") == ""
    assert f.feed("ink>秘密") == ""
    assert f.feed("</th") == ""
    assert f.feed("ink>完成") == "完成"


def test_think_filter_multiple_blocks():
    f = _ThinkFilter()
    assert f.feed("<think>A</think>一<think>B</think>二") == "一二"


def test_think_filter_holdback_released_on_flush():
    f = _ThinkFilter()
    assert f.feed("正文<") == "正文"     # "<" 疑似标签前缀，留缓冲
    assert f.flush() == "<"             # 流结束确认不是标签，放行


def test_think_filter_unclosed_think_dropped_at_flush():
    f = _ThinkFilter()
    assert f.feed("<think>没说完") == ""
    assert f.flush() == ""              # 流中断在思考内，丢弃


def test_think_filter_plain_text_passthrough():
    f = _ThinkFilter()
    assert f.feed("a<b") == "a<b"


def test_openai_chat_strips_think_block():
    p = make_openai()
    p.client = FakeOpenAIClient(SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop",
                                 message=SimpleNamespace(
                                     content="<think>推理过程</think>\n\n你好",
                                     tool_calls=None))],
        usage=SimpleNamespace(prompt_tokens=9, completion_tokens=9)))
    assert p.chat([UserMessage("x")]).text == "你好"


def test_openai_chat_think_only_yields_none():
    p = make_openai()
    p.client = FakeOpenAIClient(SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop",
                                 message=SimpleNamespace(
                                     content="<think>只有思考没有正文</think>",
                                     tool_calls=None))],
        usage=SimpleNamespace(prompt_tokens=9, completion_tokens=9)))
    assert p.chat([UserMessage("x")]).text is None


def test_openai_chat_stream_think_split_not_leaked():
    p = make_openai()
    p.client = FakeOpenAIStreamClient([
        _oai_chunk(content="<th"),
        _oai_chunk(content="ink>隐"),
        _oai_chunk(content="秘</th"),
        _oai_chunk(content="ink>你好"),
        _oai_chunk(finish="stop"),
    ])
    seen = []
    resp = p.chat_stream([UserMessage("x")], on_text=seen.append)
    assert resp.text == "你好"
    assert "".join(seen) == resp.text      # 拼接一致不变量
    assert "隐" not in "".join(seen)


def test_create_provider_bad_base_url_friendly_exit():
    from agent.config import AgentConfig
    from agent.providers import create_provider
    cfg = AgentConfig(model="m", api_key="k", base_url="https://api:minimaxi:com/v1")
    with pytest.raises(SystemExit, match="base_url"):
        create_provider(cfg)
