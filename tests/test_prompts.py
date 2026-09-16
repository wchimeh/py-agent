# @File:     test_prompts.py
# @DateTime: 2026/09/16
"""Prompt loader 单测：版本发现、默认最新、pin、错误信息。纯本地文件逻辑，零网络。"""
import pytest

from agent.prompts import PromptError, latest_version, load_prompt


def test_load_system_default():
    assert "运行在终端里的编码助手" in load_prompt("system")

def test_latest_version_system_is_2():
    # P16 M2：system_v2（注入防御）落地后默认版本为 2
    assert latest_version("system") == 2

def test_system_v2_injection_defense_clause():
    v2 = load_prompt("system")
    assert "不可信" in v2 and "不执行" in v2      # 外部内容不可信边界

def test_system_v2_drops_hardcoded_windows():
    v2 = load_prompt("system")
    assert "cmd.exe" not in v2                     # 环境说明改为运行时注入
    assert "环境说明" in v2

def test_load_system_pin_v1():
    assert "运行在终端里的编码助手" in load_prompt("system", version=1)
    assert "cmd.exe" in load_prompt("system", version=1)   # v1 保留硬编码环境行（可回退）

def test_load_missing_version_lists_available():
    with pytest.raises(PromptError) as exc:
        load_prompt("system", version=99)
    assert "v99" in str(exc.value) and "v1" in str(exc.value)  # 报错列出可用版本

def test_default_selects_latest(tmp_path):
    (tmp_path / "system_v1.md").write_text("old", encoding="utf-8")
    (tmp_path / "system_v2.md").write_text("new", encoding="utf-8")
    assert load_prompt("system", _dir=tmp_path) == "new"
    assert latest_version("system", _dir=tmp_path) == 2

def test_load_compact_default():
    assert "会话压缩器" in load_prompt("compact")

def test_load_unknown_name_raises():
    with pytest.raises(PromptError):
        load_prompt("nope")

def test_version_normalization():
    # yaml 里写成字符串 "1" 也能 pin；0 / 非数字报清晰错误
    assert load_prompt("system", version="1") == load_prompt("system", version=1)
    with pytest.raises(PromptError, match="版本号"):
        load_prompt("system", version=0)
    with pytest.raises(PromptError, match="版本号"):
        load_prompt("system", version="abc")
