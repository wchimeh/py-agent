# -*- coding: utf-8 -*-
# @File:     __init__.py
# @Author:   mjh
# @DateTime: 2026/03/14/15:36
from ..config import AgentConfig
from .openai_provider import OpenAIProvider
from .anthropic_provider import AnthropicProvider
from .retry import RetryableProvider


def create_provider(cfg: AgentConfig):
    if not cfg.api_key:
        raise SystemExit(f"[config] 缺少 API key（环境变量未设置）")
    if cfg.provider == "anthropic":
        inner = AnthropicProvider(cfg.model, cfg.api_key, cfg.base_url, cfg.max_tokens, cfg.request_timeout)
    else:
        inner = OpenAIProvider(cfg.model, cfg.api_key, cfg.base_url, cfg.max_tokens, cfg.request_timeout)

    return RetryableProvider(inner, cfg.retry_max_attempts, cfg.retry_base_delay)

