# @File:     team.py
# @DateTime: 2026/09/17
"""团队编排（P17 M3）：TaskBoard + 任务板四工具 + SpawnTool 派 worker。
worker 生命周期=回合内包干（复用子代理装配路径，无后台线程）；
并发由 loop CHILD_TOOLS 分组承担（leader 同回合 Spawn×N 进同一线程池）。"""
import threading
from dataclasses import dataclass, field
from datetime import datetime
from itertools import count
from typing import ClassVar

from .base import Tool, ToolError
from .registry import register

VALID_STATUS = ("pending", "in_progress", "completed")
TEAM_TOOL_NAMES = {"Spawn", "TaskCreate", "TaskList", "TaskUpdate", "SendMessage"}


@dataclass
class Task:
    id: int
    subject: str
    description: str = ""
    status: str = "pending"
    owner: str = ""
    updated: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))


class TaskBoard:
    """任务板：线程锁；自增 id；状态流转校验；leader 收件箱。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._tasks: dict[int, Task] = {}
        self._seq = count(1)
        self.inbox: list[dict] = []

    def create(self, subject: str, description: str = "") -> Task:
        with self._lock:
            t = Task(id=next(self._seq), subject=subject, description=description)
            self._tasks[t.id] = t
            return t

    def list_tasks(self) -> list[Task]:
        with self._lock:
            return list(self._tasks.values())

    def update(self, task_id: int, status: str | None = None,
               owner: str | None = None) -> Task | None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return None
            if status is not None:
                if status not in VALID_STATUS:
                    raise ValueError(f"非法状态 {status}（可选 {'/'.join(VALID_STATUS)}）")
                t.status = status
            if owner is not None:
                t.owner = owner
            t.updated = datetime.now().strftime("%H:%M:%S")
            return t

    def message(self, content: str, sender: str) -> None:
        with self._lock:
            self.inbox.append({"sender": sender, "content": content,
                               "ts": datetime.now().strftime("%H:%M:%S")})

    def summary(self) -> str:
        tasks = self.list_tasks()
        if not tasks:
            return "（任务板为空）"
        rows = [f"  #{t.id} [{t.status}] {t.subject}"
                + (f"（{t.owner}）" if t.owner else "") for t in tasks]
        return f"任务板（{len(tasks)} 项）：\n" + "\n".join(rows)

    def inbox_summary(self) -> str:
        if not self.inbox:
            return "（收件箱为空）"
        rows = [f"  [{m['ts']}] {m['sender']}: {m['content'][:80]}" for m in self.inbox]
        return f"leader 收件箱（{len(self.inbox)} 条）：\n" + "\n".join(rows)


# ---- 模块级装配（sandbox.set_executor 同款全局模式）----

_BOARD = TaskBoard()
_journal = None
TEAM_MAX_WORKERS = 3
_active_lock = threading.Lock()
_active = 0                       # 在跑 worker 数（并发上限约束用）
_worker_name = threading.local()  # worker 身份（线程局部：并行 worker 各自可见）


def get_board() -> TaskBoard:
    return _BOARD


def set_worker(name: str | None) -> None:
    _worker_name.name = name


def current_worker() -> str:
    return getattr(_worker_name, "name", None) or "leader"


def configure(journal=None, max_workers: int = 3) -> None:
    global _journal, TEAM_MAX_WORKERS
    _journal = journal
    TEAM_MAX_WORKERS = max_workers


def reset() -> None:
    global _BOARD, _journal, TEAM_MAX_WORKERS, _active
    _BOARD = TaskBoard()
    _journal = None
    TEAM_MAX_WORKERS = 3
    with _active_lock:
        _active = 0
    set_worker(None)


def _log(event: str, **fields) -> None:
    if _journal is not None:
        _journal.log(event, agent="team", **fields)


def leader_system_note() -> str:
    return ("\n\n[leader 模式] 用户以 /team 下达目标 = 明确要求团队编排。本说明覆盖"
            "系统提示中'仅大任务值得派子代理'的一般性指引：执行类工作（读文件/搜索/"
            "统计/跑命令）必须用 Spawn 派 worker 完成（可同回合派多个并发），"
            "leader 不要自己动手执行这些操作，派工前也无需先亲自探查目录"
            "（工作区根见环境说明，直接写进任务书）。leader 职责只有三步：拆解目标 → "
            "Spawn 派工（worker 只读、跑完回灌报告）→ 汇总各 worker 报告给用户；"
            "TaskList 可看任务板进度。")


# ---- 任务板四工具 ----

@register
class TaskCreateTool(Tool):
    name = "TaskCreate"
    description = "在团队任务板上创建任务（自增编号，初始 pending）。"
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "任务标题（一行）"},
            "description": {"type": "string", "description": "任务详情（可选）"},
        },
        "required": ["subject"],
    }

    def execute(self, subject: str, description: str = "") -> str:
        t = _BOARD.create(subject, description)
        _log("task_update", action="create", task=t.id, subject=subject[:60])
        return f"已创建任务 {t.id}：{subject}（pending）"


@register
class TaskListTool(Tool):
    name = "TaskList"
    description = "查看团队任务板全部任务（编号/状态/标题/owner）。"
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def execute(self) -> str:
        return _BOARD.summary()


@register
class TaskUpdateTool(Tool):
    name = "TaskUpdate"
    description = "更新任务状态/归属。status 可选 pending|in_progress|completed。"
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "description": "任务编号"},
            "status": {"type": "string", "description": "pending|in_progress|completed"},
            "owner": {"type": "string", "description": "认领人（可选）"},
        },
        "required": ["task_id"],
    }

    def execute(self, task_id: int, status: str | None = None, owner: str | None = None) -> str:
        try:
            t = _BOARD.update(task_id, status=status, owner=owner)
        except ValueError as e:
            raise ToolError(f"任务 {task_id} {e}") from None
        if t is None:
            raise ToolError(f"任务 {task_id} 不存在，请先 TaskCreate 或用 TaskList 查编号")
        _log("task_update", action="update", task=task_id, status=t.status)
        return f"任务 {task_id} 已更新：[{t.status}]{f'（{t.owner}）' if t.owner else ''}"


@register
class SendMessageTool(Tool):
    name = "SendMessage"
    description = "向 leader 收件箱留档一条进度备注（worker 完成任务后汇报用）。"
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "消息内容"},
        },
        "required": ["content"],
    }

    def execute(self, content: str) -> str:
        sender = current_worker()
        _BOARD.message(content, sender=sender)
        _log("message", sender=sender, content=content[:60])
        return f"已留档（{sender}）：{content[:60]}"


@register
class SpawnTool(Tool):
    name = "Spawn"
    description = ("派一个 worker 完成任务并返回其报告（仅 leader 可用）。worker 只读、"
                   "带任务板三件工具、跑完即回收不跨回合存活；同回合 Spawn 多个即并发。"
                   "在跑 worker 数受 team.max_workers 限制。")
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "worker_name": {"type": "string", "description": "worker 名字（如 探查员/测试员）"},
            "first_task_subject": {"type": "string", "description": "首个任务标题（自动建档置 in_progress）"},
            "first_task_prompt": {"type": "string", "description": "完整任务书（worker 看不到主会话历史）"},
        },
        "required": ["worker_name", "first_task_subject", "first_task_prompt"],
    }

    def execute(self, worker_name: str, first_task_subject: str, first_task_prompt: str) -> str:
        global _active
        with _active_lock:
            if _active >= TEAM_MAX_WORKERS:
                return (f"[Spawn 拒绝] 在跑 worker 已达上限（team.max_workers={TEAM_MAX_WORKERS}）："
                        "请等本轮 worker 收工后再派，或减小派工粒度")
            _active += 1
        try:
            t = _BOARD.create(first_task_subject, description=first_task_prompt[:200])
            _BOARD.update(t.id, status="in_progress", owner=worker_name)
            from .subagent import spawn  # 复用子代理装配路径（与 Agent 同构）
            note = (f"\n\n[worker 身份] 你是 worker「{worker_name}」：任务 #{t.id}"
                    f"「{first_task_subject}」已置 in_progress（跑完系统自动置 completed，"
                    "无需手动流转）；完成后 SendMessage 留档进度，最后输出自包含报告（结论 + 关键证据）")
            set_worker(worker_name)
            try:
                report = spawn(description=f"{worker_name}·{first_task_subject}",
                               prompt=first_task_prompt,
                               extra_tools={"TaskList", "TaskUpdate", "SendMessage"},
                               system_note=note)
            finally:
                set_worker(None)
            # 兜底流转：worker 回合内包干，正常返回即完成；终止报告不算完成（如实留 in_progress）
            if "[任务终止]" not in report:
                _BOARD.update(t.id, status="completed")
            _log("spawn", worker=worker_name, task=t.id, subject=first_task_subject[:60])
            return report
        finally:
            with _active_lock:
                _active -= 1
