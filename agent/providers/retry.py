# @File:     retry.py
# @Author:   mjh
# @DateTime: 2026/03/15/10:03
import random
import time

from anthropic import APIConnectionError as AntConn
from anthropic import APIStatusError as AntStatus
from anthropic import APITimeoutError as AntTimeout
from openai import APIConnectionError as OAIConn
from openai import APIStatusError as OAIStatus
from openai import APITimeoutError as OAITimeout

from .base import LLMProvider, LLMResponse, Message, ToolDef

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class AgentError(Exception):

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.retryable = retryable


def is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (OAIConn, OAITimeout, AntTimeout, AntConn)):
        return True
    if isinstance(exc, (OAIStatus, AntStatus)):
        return exc.status_code in RETRYABLE_STATUS
    return False


def friendly(exc: Exception) -> str:

    code = getattr(exc, 'status_code', None)
    if code == 401:
        return "API key 无效或未授权（401），检查 config.yaml 的 api_key"
    if code == 403:
        return "无权限访问该模型/接口（403）"
    if code == 404:
        return "模型或接口不存在（404），检查 config.yaml 的 model/base_url"
    if code == 429:
        return "触发限流（429），请稍后重试"
    if code is not None:
        return f"服务端错误（HTTP {code}）"
    if isinstance(exc, (OAITimeout, AntTimeout)):
        return "请求超时，可调大 config.yaml 的 request_timeout"
    if isinstance(exc, (OAIConn, AntConn)):
        return "网络连接失败，检查网络或 base_url"
    return f"{type(exc).__name__}: {exc}"


class RetryableProvider(LLMProvider):

    def __init__(self, inner: LLMProvider, max_attempts: int = 3, base_delay: float = 1.0,
                 max_delay: float = 30.0, sleep=time.sleep, retryable=is_retryable):
        self.inner = inner
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.sleep = sleep  # 测试注入 fake sleep，禁止真实等待
        self.retryable = retryable  # 测试注入假判定器

    def chat(self, messages: list[Message], tools: list[ToolDef] | None = None,
             system: str = "") -> LLMResponse:
        return self._retry(lambda: self.inner.chat(messages, tools, system))

    def chat_stream(self, messages: list[Message], tools: list[ToolDef] | None = None,
                    system: str = "", on_text=None) -> LLMResponse:
        return self._retry(lambda: self.inner.chat_stream(messages, tools, system,
                                                          on_text=on_text))

    def _retry(self, call) -> LLMResponse:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return call()
            except Exception as e:
                last_exc = e
                if not self.retryable(e) or attempt == self.max_attempts:
                    break
                delay = min(self.base_delay * 2 ** (attempt - 1), self.max_delay)
                delay *= random.uniform(0.8, 1.2)  # 抖动，防止集中重试
                self.sleep(delay)
        msg = friendly(last_exc)
        if self.retryable(last_exc):
            msg += f"（已重试 {attempt} 次仍失败）"
        raise AgentError(msg, retryable=self.retryable(last_exc))

    def __getattr__(self, name):
        # 委托 inner 的属性（model 等）
        return getattr(self.inner, name)




