# -*- coding: utf-8 -*-
# @File:     config.py
# @Author:   mjh
# @DateTime: 2026/03/14/15:33
import os
from dataclasses import dataclass
import yaml
# from dotenv import load_dotenv

@dataclass
class AgentConfig:
    provider: str = "openai"
    model: str = ""
    api_key: str = ""
    base_url: str | None = None
    max_tokens: int = 4096
    request_timeout: int = 120
    retry_max_attempts: int = 3
    retry_base_delay: float = 1.0
    max_turns: int = 25
    token_budget: int = 500000
    permission_mode: str = "default"

    context_window: int = 128000
    compact_threshold: float = 0.8
    keep_recent: int = 8

    journal: bool = True
    save_session: bool = True
    workspace_root: str = ""


def load_config(path: str = "config.yaml") -> AgentConfig:
    # load_dotenv()
    data = yaml.safe_load(open(path, encoding="utf-8")) \
        if os.path.exists(path) else {}
    provider = data.get("provider", "openai")
    # env_key = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
    mode = data.get("permission_mode", "default")
    if mode not in ("default", "acceptEdits", "bypass"):
        raise SystemExit(f"[config] 非法 permission_mode: {mode}（可选 default/acceptEdits/bypass）")

    context_window = data.get("context_window", 128000)
    compact_threshold = data.get("compact_threshold", 0.8)
    keep_recent = data.get("keep_recent", 8)
    if not 0 < compact_threshold < 1:
        raise SystemExit(f"[config] 非法 compact_threshold: {compact_threshold}（须 0~1 之间）")
    if keep_recent < 2:
        raise SystemExit(f"[config] 非法 keep_recent: {keep_recent}（须 >= 2）")

    return AgentConfig(provider=provider, model=data.get("model", ""), api_key=data.get("api_key"),
                       base_url=data.get("base_url"), max_tokens=data.get("max_tokens", 4096),
                       request_timeout=data.get("request_timeout", 120),
                       retry_max_attempts=data.get("retry_max_attempts", 3),
                       retry_base_delay=data.get("retry_base_delay", 1.0),
                       max_turns=data.get("max_turns", 25),
                       token_budget=data.get("token_budget", 500000),
                       permission_mode=mode,
                       context_window=context_window,
                       compact_threshold=compact_threshold,
                       keep_recent=keep_recent,
                       journal=data.get("journal", True),
                       save_session=data.get("save_session", True),
                       workspace_root=data.get("workspace_root", "")
                       )



