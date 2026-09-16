# @File:     test_retry.py
# @Author:   mjh
# @DateTime: 2026/03/15
"""RetryableProvider 单测：注入 fake sleep 与假判定器，全程无网络、无真实等待。"""
import pytest

from agent.providers.base import LLMResponse, StopReason
from agent.providers.retry import AgentError, RetryableProvider, friendly, is_retryable


class RetryErr(Exception):
    """假的可重试错误"""


class FatalErr(Exception):
    """假的不可重试错误"""


class Flaky:
    """按脚本依次抛异常，脚本耗尽后返回成功响应"""

    def __init__(self, failures):
        self.failures, self.calls = list(failures), 0

    def chat(self, messages, tools=None, system=""):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return LLMResponse(text="ok", tool_calls=[], stop_reason=StopReason.END_TURN)


def make(flaky, sleeps):
    return RetryableProvider(flaky, max_attempts=3, base_delay=1.0,
                             sleep=sleeps.append,
                             retryable=lambda e: isinstance(e, RetryErr))


# ---------- 重试成功路径 ----------

def test_retry_succeeds_after_two_failures():
    flaky = Flaky([RetryErr(), RetryErr()])
    sleeps: list[float] = []
    resp = make(flaky, sleeps).chat([])
    assert resp.text == "ok"
    assert flaky.calls == 3

def test_backoff_is_exponential_with_jitter():
    flaky = Flaky([RetryErr(), RetryErr()])
    sleeps: list[float] = []
    make(flaky, sleeps).chat([])
    # 基数 1s、2s，抖动 [0.8, 1.2] 倍 —— 只断言区间，禁止等号
    assert 0.8 <= sleeps[0] <= 1.2
    assert 1.6 <= sleeps[1] <= 2.4

def test_backoff_respects_max_delay():
    flaky = Flaky([RetryErr()] * 2)
    sleeps: list[float] = []
    p = RetryableProvider(flaky, max_attempts=3, base_delay=20.0, max_delay=30.0,
                          sleep=sleeps.append,
                          retryable=lambda e: isinstance(e, RetryErr))
    p.chat([])
    assert all(16.0 <= s <= 36.0 for s in sleeps)   # 20、40→截到 30，均带抖动


# ---------- 不可重试与耗尽 ----------

def test_non_retryable_fails_fast_without_sleep():
    flaky = Flaky([FatalErr()])
    sleeps: list[float] = []
    with pytest.raises(AgentError) as ei:
        make(flaky, sleeps).chat([])
    assert ei.value.retryable is False
    assert flaky.calls == 1          # 一次都不重试
    assert sleeps == []

def test_retry_exhausted_raises_agent_error():
    flaky = Flaky([RetryErr()] * 5)
    sleeps: list[float] = []
    p = make(flaky, sleeps)
    with pytest.raises(AgentError) as ei:
        p.chat([])
    assert ei.value.retryable is True
    assert flaky.calls == 3          # 恰好尝试 max_attempts 次
    assert "已重试 3 次" in str(ei.value)

def test_agent_error_carries_friendly_message():
    flaky = Flaky([FatalErr("boom")])
    sleeps: list[float] = []
    with pytest.raises(AgentError) as ei:
        make(flaky, sleeps).chat([])
    assert "FatalErr" in str(ei.value) and "boom" in str(ei.value)


# ---------- 真实 SDK 异常的判定与分类 ----------

def _sdk_status_error(cls, code: int):
    import httpx
    req = httpx.Request("GET", "https://example.com")
    return cls("http error", response=httpx.Response(code, request=req), body=None)

def test_is_retryable_real_openai_errors():
    from openai import APIConnectionError, APIStatusError
    assert is_retryable(_sdk_status_error(APIStatusError, 429)) is True
    assert is_retryable(_sdk_status_error(APIStatusError, 503)) is True
    assert is_retryable(_sdk_status_error(APIStatusError, 401)) is False
    assert is_retryable(_sdk_status_error(APIStatusError, 400)) is False
    import httpx
    assert is_retryable(APIConnectionError(
        request=httpx.Request("GET", "https://example.com"))) is True

def test_is_retryable_real_anthropic_errors():
    from anthropic import APIStatusError
    assert is_retryable(_sdk_status_error(APIStatusError, 429)) is True
    assert is_retryable(_sdk_status_error(APIStatusError, 401)) is False

def test_friendly_messages_by_status():
    from openai import APIStatusError
    assert "401" in friendly(_sdk_status_error(APIStatusError, 401))
    assert "404" in friendly(_sdk_status_error(APIStatusError, 404))
    assert "429" in friendly(_sdk_status_error(APIStatusError, 429))
    assert "500" in friendly(_sdk_status_error(APIStatusError, 500))

def test_401_through_wrapper_is_friendly_and_fast():
    from openai import APIStatusError
    flaky = Flaky([_sdk_status_error(APIStatusError, 401)])
    sleeps: list[float] = []
    with pytest.raises(AgentError) as ei:
        make(flaky, sleeps).chat([])     # make 的假判定器对 SDK 401 返回 False
    assert "401" in str(ei.value)
    assert flaky.calls == 1 and sleeps == []


# ---------- 属性委托 ----------

def test_getattr_delegates_to_inner():
    class WithModel:
        model = "test-model"
        def chat(self, messages, tools=None, system=""):
            return LLMResponse(text="x", tool_calls=[], stop_reason=StopReason.END_TURN)
    p = RetryableProvider(WithModel(), sleep=lambda s: None)
    assert p.model == "test-model"


# ---------- P8：chat_stream 重试 ----------

class FlakyStream(Flaky):
    def __init__(self, failures):
        super().__init__(failures)
        self.stream_calls = 0

    def chat_stream(self, messages, tools=None, system="", on_text=None):
        self.stream_calls += 1
        if self.failures:
            raise self.failures.pop(0)
        on_text("ok")
        return LLMResponse(text="ok", tool_calls=[], stop_reason=StopReason.END_TURN)


def test_retry_chat_stream_success_after_failure():
    seen: list[str] = []
    flaky = FlakyStream([RetryErr()])
    sleeps: list[float] = []
    resp = make(flaky, sleeps).chat_stream([], on_text=seen.append)

    assert resp.text == "ok"
    assert flaky.stream_calls == 2             # 失败一次后重试成功
    assert seen == ["ok"]                      # 增量只来自成功的那次尝试
