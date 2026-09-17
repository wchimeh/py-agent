# @File:     test_team.py
# @DateTime: 2026/09/17
"""团队编排（P17 M3）：TaskBoard 四工具 + SendMessage + SpawnTool，Fake provider 零网络。"""
import pytest

from agent.permissions import PermissionGate
from agent.providers.base import LLMResponse, StopReason, ToolCall
from agent.tools.base import ToolError

BYPASS_GATE = PermissionGate("bypass")


class FakeProvider:
    def __init__(self, script):
        self.script = list(script)
        self.seen_tools = []

    def chat(self, messages, tools=None, system=""):
        self.seen_tools.append([t.name for t in (tools or [])])
        return self.script.pop(0)


def _end_resp(text="worker 报告"):
    return LLMResponse(text=text, tool_calls=[], stop_reason=StopReason.END_TURN,
                       prompt_tokens=10, completion_tokens=5)


def _tool_resp(calls):
    return LLMResponse(text=None, tool_calls=calls, stop_reason=StopReason.TOOL_USE,
                       prompt_tokens=10, completion_tokens=2)


@pytest.fixture(autouse=True)
def _reset():
    from agent.tools import subagent, team
    subagent.reset()
    team.reset()
    yield
    subagent.reset()
    team.reset()


def _tool(name):
    from agent.tools.registry import _TOOLS
    return _TOOLS[name]


# ---------- TaskBoard ----------

def test_board_create_and_list():
    from agent.tools.team import TaskBoard
    b = TaskBoard()
    t = b.create("探查仓库", description="找出模块划分")
    assert t.id == 1 and t.status == "pending"
    t2 = b.create("写报告")
    assert t2.id == 2                            # 自增 id
    assert [x.subject for x in b.list_tasks()] == ["探查仓库", "写报告"]


def test_board_update_status_and_owner():
    from agent.tools.team import TaskBoard
    b = TaskBoard()
    b.create("任务A")
    b.update(1, status="in_progress", owner="worker-1")
    got = b.list_tasks()[0]
    assert got.status == "in_progress" and got.owner == "worker-1"
    assert b.update(99) is None                  # 未知 id → None
    with pytest.raises(ValueError):
        b.update(1, status="done")               # 非法状态拒


def test_board_message_inbox():
    from agent.tools.team import TaskBoard
    b = TaskBoard()
    b.message("完成探查，发现 3 模块", sender="worker-1")
    assert len(b.inbox) == 1
    assert b.inbox[0]["sender"] == "worker-1"
    assert "3 模块" in b.inbox[0]["content"]


# ---------- 四工具 ----------

def test_task_create_tool():
    from agent.tools.team import get_board
    out = _tool("TaskCreate").execute(subject="探查仓库", description="找出模块划分")
    assert "已创建任务 1" in out and "探查仓库" in out
    assert get_board().list_tasks()[0].subject == "探查仓库"


def test_task_list_tool():
    _tool("TaskCreate").execute(subject="任务A")
    _tool("TaskCreate").execute(subject="任务B", description="细节")
    out = _tool("TaskList").execute()
    assert "任务A" in out and "任务B" in out and "pending" in out


def test_task_update_tool_flow():
    _tool("TaskCreate").execute(subject="任务A")
    out = _tool("TaskUpdate").execute(task_id=1, status="in_progress", owner="worker-1")
    assert "in_progress" in out
    out = _tool("TaskUpdate").execute(task_id=1, status="completed")
    assert "completed" in out
    with pytest.raises(ToolError, match="不存在"):
        _tool("TaskUpdate").execute(task_id=9, status="completed")
    with pytest.raises(ToolError, match="状态"):
        _tool("TaskUpdate").execute(task_id=1, status="done")


def test_send_message_tool_inbox():
    from agent.tools.team import get_board, set_worker
    set_worker("worker-2")
    out = _tool("SendMessage").execute(content="探查完成，共 3 模块")
    assert "worker-2" in out                     # 回执带身份
    assert get_board().inbox[-1]["sender"] == "worker-2"
    set_worker(None)


def test_worker_identity_thread_local_default():
    from agent.tools.team import current_worker
    assert current_worker() == "leader"          # 未设置时=leader


# ---------- SpawnTool ----------

def _configure_sub(provider, **kw):
    from agent.tools import subagent
    subagent.configure(provider=provider, parent_gate=BYPASS_GATE,
                       max_turns=kw.get("max_turns", 10),
                       token_slice=kw.get("token_slice", 50000))


def test_spawn_tool_runs_worker_flow(ws):
    from agent.tools.team import get_board
    upd = ToolCall(id="t1", name="TaskUpdate",
                   arguments={"task_id": 1, "status": "completed"})
    msg = ToolCall(id="t2", name="SendMessage", arguments={"content": "探查完成：3 模块"})
    provider = FakeProvider([_tool_resp([upd]), _tool_resp([msg]), _end_resp("worker 结论")])
    _configure_sub(provider)
    out = _tool("Spawn").execute(worker_name="探查员", first_task_subject="探查仓库",
                                 first_task_prompt="总结仓库结构")
    assert "worker 结论" in out                       # 报告回灌
    t = get_board().list_tasks()[0]                   # 任务已建档并流转
    assert t.subject == "探查仓库" and t.status == "completed" and t.owner == "探查员"
    assert get_board().inbox[-1]["sender"] == "探查员"   # SendMessage 以 worker 身份留档


def test_spawn_auto_completes_without_worker_update(ws):
    from agent.tools.team import get_board
    provider = FakeProvider([_end_resp("干完活直接报告，没碰任务板")])
    _configure_sub(provider)
    _tool("Spawn").execute(worker_name="实干家", first_task_subject="统计行数",
                           first_task_prompt="p")
    t = get_board().list_tasks()[0]
    assert t.status == "completed"                  # Spawn 返回即兜底置完成，不依赖模型仪式感


def test_spawn_terminated_worker_keeps_in_progress(ws):
    from agent.tools.team import get_board
    glob = ToolCall(id="g1", name="Glob", arguments={"pattern": "*.txt"})
    provider = FakeProvider([_tool_resp([glob]), _tool_resp([glob])])
    _configure_sub(provider, max_turns=1)           # 两轮工具即触顶终止
    out = _tool("Spawn").execute(worker_name="半途", first_task_subject="找文件",
                                 first_task_prompt="p")
    assert "[任务终止]" in out
    assert get_board().list_tasks()[0].status == "in_progress"   # 未完成不谎报 completed


def test_spawn_worker_tools_whitelist(ws):
    provider = FakeProvider([_end_resp("ok")])
    _configure_sub(provider)
    _tool("Spawn").execute(worker_name="w", first_task_subject="s", first_task_prompt="p")
    names = set(provider.seen_tools[0])
    assert {"TaskList", "TaskUpdate", "SendMessage"} <= names   # worker 带任务板三件
    assert "Spawn" not in names and "Agent" not in names        # 防递归


def test_spawn_exceeds_max_workers_rejects(ws):
    import threading

    from agent.tools import team
    team.configure(max_workers=1)
    release = threading.Event()
    started = threading.Event()

    class Blocky:
        def chat(self, messages, tools=None, system=""):
            started.set()
            release.wait(2)
            return _end_resp("慢工结论")

    _configure_sub(Blocky())

    def bg():
        _tool("Spawn").execute(worker_name="慢", first_task_subject="a", first_task_prompt="p")

    th = threading.Thread(target=bg)
    th.start()
    assert started.wait(2)                            # 慢 worker 占住唯一名额
    out = _tool("Spawn").execute(worker_name="快", first_task_subject="b", first_task_prompt="p")
    assert "上限" in out                              # 超限友好拒，不抛异常
    release.set()
    th.join()


def test_same_turn_two_spawns_parallel(ws):
    import time

    from agent.loop import AgentLoop

    class Slow:
        def chat(self, messages, tools=None, system=""):
            time.sleep(0.6)
            return _end_resp("结论")

    _configure_sub(Slow())
    calls = [ToolCall(id=f"s{i}", name="Spawn",
                      arguments={"worker_name": f"w{i}", "first_task_subject": f"t{i}",
                                 "first_task_prompt": "p"}) for i in range(2)]
    main = FakeProvider([_tool_resp(calls), _end_resp("leader 汇总")])
    loop = AgentLoop(main, permission_gate=BYPASS_GATE)
    t0 = time.time()
    assert loop.run("团队上") == "leader 汇总"
    assert time.time() - t0 < 1.1                     # 串行 ≥1.2s


# ---------- leader 说明 / 收件箱渲染 / config ----------

def test_leader_system_note():
    from agent.tools.team import leader_system_note
    note = leader_system_note()
    assert "Spawn" in note and "leader" in note      # leader 能力说明
    assert "TaskList" in note
    assert "必须" in note and "不要自己动手" in note   # 指令式派工（/team 即用户编排意图）
    assert "覆盖" in note                            # 压过 system_v3 的"仅大任务值得派"
    assert "无需先亲自探查" in note                   # 不许先 Bash/ls 探目录再派工


def test_board_inbox_summary_render():
    from agent.tools.team import TaskBoard
    b = TaskBoard()
    assert b.inbox_summary() == "（收件箱为空）"
    b.message("探查完成", sender="worker-1")
    text = b.inbox_summary()
    assert "worker-1" in text and "探查完成" in text


def test_config_team_section(tmp_path):
    from agent.config import load_config
    p = tmp_path / "config.yaml"
    p.write_text("", encoding="utf-8")
    assert load_config(str(p)).team_max_workers == 3   # 缺省
    p.write_text("team:\n  max_workers: 2\n", encoding="utf-8")
    assert load_config(str(p)).team_max_workers == 2
    p.write_text("team:\n  max_workers: 0\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="team"):
        load_config(str(p))
