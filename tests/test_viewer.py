# @File:     test_viewer.py
# @DateTime: 2026/09/17
"""子代理查看器（P17 M2）：Registry + 渲染纯函数，零网络零终端。"""
from agent.viewer import SubagentRegistry, browse, render_menu, render_transcript


def _reg():
    r = SubagentRegistry()
    r.register("sub-1", "探查仓库结构")
    r.add_lines("sub-1", "● Read(a.py)", "  ✓ 0.1s", "结论：3 个模块")
    return r


# ---------- SubagentRegistry ----------

def test_registry_register_and_snapshot():
    r = _reg()
    snap = r.snapshot()
    assert len(snap) == 1
    assert snap[0]["id"] == "sub-1"
    assert snap[0]["description"] == "探查仓库结构"
    assert snap[0]["status"] == "running"
    assert snap[0]["lines"] == 3          # 行数计数


def test_registry_update_status():
    r = _reg()
    r.update("sub-1", status="done")
    assert r.snapshot()[0]["status"] == "done"


def test_registry_transcript_bounded():
    r = SubagentRegistry()
    r.register("sub-1", "d")
    r.add_lines("sub-1", *(f"line{i}" for i in range(2500)))
    assert len(r.get_lines("sub-1")) == 2000   # 有界防内存膨胀
    assert r.get_lines("sub-1")[-1] == "line2499"


def test_registry_snapshot_is_copy():
    r = _reg()
    snap = r.snapshot()
    snap[0]["status"] = "hacked"
    assert r.snapshot()[0]["status"] == "running"   # 快照不影响内部状态


# ---------- 渲染纯函数 ----------

def test_render_menu_lists_agents():
    r = _reg()
    r.register("sub-2", "验证测试覆盖")
    text = render_menu(r.snapshot())
    assert "sub-1" in text and "探查仓库结构" in text
    assert "sub-2" in text and "running" in text
    assert "3 行" in text


def test_render_menu_empty():
    assert render_menu([]) == "（暂无子代理记录）"


def test_render_transcript_tails_40():
    lines = [f"line{i}" for i in range(100)]
    text = render_transcript(lines, title="sub-1 探查仓库结构")
    assert "line99" in text            # 尾部保留
    assert "line60" in text            # 恰好进尾部窗口（0..99 取尾 40 = 60..99）
    assert "line59" not in text        # 窗口外丢弃
    assert "sub-1 探查仓库结构" in text


def test_browse_menu_and_selection():
    r = _reg()
    r.register("sub-2", "第二个")
    menu = browse(r)
    assert "sub-1" in menu and "sub-2" in menu          # 无编号=菜单
    detail = browse(r, selection=1)
    assert "结论：3 个模块" in detail                    # 有编号=该代理 transcript
    assert "编号超出范围" in browse(r, selection=9)      # 越界友好提示
