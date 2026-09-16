# @File:     __init__.py
# @Author:   mjh
# @DateTime: 2026/03/14/15:36
from ..config import AgentConfig
from .anthropic_provider import AnthropicProvider
from .openai_provider import OpenAIProvider
from .retry import RetryableProvider


def create_provider(cfg: AgentConfig):
    if not cfg.api_key:
        raise SystemExit("[config] 缺少 API key（环境变量未设置）")
    cls = AnthropicProvider if cfg.provider == "anthropic" else OpenAIProvider
    try:
        inner = cls(cfg.model, cfg.api_key, cfg.base_url, cfg.max_tokens, cfg.request_timeout)
    except Exception as e:
        # 典型：base_url 打错（如 api:minimaxi:com）→ SDK 抛 InvalidURL，11 层 traceback 对用户无意义
        raise SystemExit(f"[config] 创建 {cfg.provider} provider 失败：{e}\n"
                         f"          请检查 config.yaml 的 base_url（形如 https://api.minimaxi.com/v1）与 api_key") from e

    return RetryableProvider(inner, cfg.retry_max_attempts, cfg.retry_base_delay)

