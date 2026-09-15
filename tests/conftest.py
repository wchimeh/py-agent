# -*- coding: utf-8 -*-
# @File:     conftest.py
# @Author:   mjh
# @DateTime: 2026/03/14
import sys
from pathlib import Path

import pytest

# 保证任意方式启动 pytest 都能导入项目根下的 agent 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """把工作区根设为 tmp_path：写类工具（Write/Edit）测试用。"""
    from agent.tools import workspace
    monkeypatch.setattr(workspace, "WORKSPACE_ROOT", str(tmp_path))
    return tmp_path
