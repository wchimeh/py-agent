# @File:     main.py
# @Author:   mjh
# @DateTime: 2026/03/14/11:35
import os
import sys

from prompt_toolkit import prompt

from agent.budget import BudgetPool
from agent.config import load_config
from agent.journal import Journal
from agent.loop import AgentLoop
from agent.mcp_client import MCPManager
from agent.permissions import PermissionGate
from agent.providers import create_provider
from agent.providers.retry import AgentError
from agent.sandbox import DockerExecutor, probe_docker, set_executor
from agent.session import (
    SessionError,
    list_sessions,
    load_session,
    new_session_id,
    save_session,
)
from agent.tools import subagent, team
from agent.tools.bash import refresh_description
from agent.tools.registry import get_tool_defs
from agent.tools.team import TEAM_TOOL_NAMES
from agent.tools.workspace import get_root, set_root
from agent.viewer import KeyListener, SubagentRegistry, browse

OLD_SESSION = ".agent/session.json"   # P6 初版单文件，已停用


def _setup_sandbox(cfg, sid: str):
    """装配沙箱执行器：docker 模式返回 executor（供会话结束清理），none 返回 None。"""
    if cfg.sandbox_mode != "docker":
        return None
    if not probe_docker():
        raise SystemExit("[sandbox] 配置了 sandbox.mode=docker 但 docker 不可用："
                         "请确认 Docker 已安装并正在运行（Windows 需 Docker Desktop/WSL2）")
    executor = DockerExecutor(name=f"agent-sandbox-{sid}", image=cfg.sandbox_image,
                              workspace_host=get_root(), memory=cfg.sandbox_memory,
                              cpus=cfg.sandbox_cpus)
    set_executor(executor)
    refresh_description()
    return executor


def _env_note(docker: bool, nt: bool = os.name == "nt") -> str:
    """运行时注入 system prompt 的环境说明行（system_v2 起不再硬编码在 prompt 文件里）。"""
    if docker:
        return ("\n  - 运行环境：Bash 命令在 Linux 容器内执行（sh，无网络），"
                "宿主工作区挂载于 /workspace；所有工具统一用 /workspace/... 路径"
                "（文件工具会自动映射到宿主工作区）")
    if nt:
        return (f"\n  - 运行环境：Windows，Bash 命令走 cmd.exe，优先使用跨平台命令；"
                f"工作区根：{get_root()}（Glob/Grep 未传 path 时在此搜索，任务范围以此为准）")
    return (f"\n  - 运行环境：Linux/macOS，Bash 命令走 POSIX shell；"
            f"工作区根：{get_root()}（Glob/Grep 未传 path 时在此搜索，任务范围以此为准）")


def _hint_resume():
    sessions = list_sessions()
    if sessions:
        print(f"[agent] 检测到 {len(sessions)} 个历史会话，/resume 恢复")
    if os.path.exists(OLD_SESSION):
        print("[agent] 发现旧版 session.json（已停用），可手动删除")


def _pick_session() -> dict | None:
    """编号选择会话；取消/无效输入返回 None。"""
    sessions = [s for s in list_sessions() if not s["corrupt"]]
    if not sessions:
        print("[agent] 没有可恢复的会话")
        return None
    print("可恢复的会话（最新在前）：")
    for i, s in enumerate(sessions[:10], 1):
        print(f"  {i}) {s['id']}  {s['first_input']} ({s['msgs']} 条)")
    try:
        answer = prompt("输入编号恢复（回车取消）: ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return None
    if not answer:
        return None
    if not answer.isdigit() or not (1 <= int(answer) <= min(len(sessions), 10)):
        print("[agent] 无效编号，已取消")
        return None
    return sessions[int(answer) - 1]


def _apply_resume(loop, session: dict) -> str:
    """加载会话到 loop 并返回其 ID；失败抛异常由调用方处理。"""
    loop.messages = load_session(session["id"])
    return session["id"]


def _run_task(loop, registry, text: str) -> None:
    """单次任务执行：挂载 Ctrl+T 监听 + 异常粗捕获（/team 与普通任务共用）。"""
    listener = KeyListener(registry)   # Ctrl+T 仅任务执行期间挂载（提示符归 prompt_toolkit）
    if not listener.start() and not start_loop._ctrl_t_hinted:
        print("[agent] Ctrl+T 查看器不可用（非交互终端），任务间可用 /agents 浏览")
        start_loop._ctrl_t_hinted = True
    try:
        result = loop.run(text)
        if not loop.last_streamed:
            print(result)   # 流式回合已逐段打印过，不再重印全文
        p, c, s = loop.last_usage
        print(f"  \n[stats] 本次 ↑{p} ↓{c} · {s:.1f}s"
              f" | 累计 ↑{loop.total_prompt_tokens} ↓{loop.total_completion_tokens}")
    except KeyboardInterrupt:  # 必须在 except Exception 之前
        print("\n[agent] 任务已打断，会话历史保留（提示符处 Ctrl+C 退出）")
    except AgentError as e:
        print(f"[错误] {e}")
    except Exception as e:  # 粗捕获保进程
        print(f"[错误] {type(e).__name__}: {e}")
    finally:
        listener.stop()   # 终端所有权归还 prompt_toolkit


def start_loop():
    start_loop._ctrl_t_hinted = False   # Ctrl+T 降级提示只打一次
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    cfg = load_config()
    set_root(cfg.workspace_root or os.getcwd())
    gate = PermissionGate(cfg.permission_mode, sandbox_trusted=cfg.sandbox_trusted)
    sid = new_session_id()
    docker_exec = _setup_sandbox(cfg, sid)
    journal = Journal.daily(keep_days=cfg.journal_keep_days) if cfg.journal else None
    provider = create_provider(cfg)
    mcp_mgr = MCPManager(cfg.mcp_servers, call_timeout=cfg.mcp_call_timeout)
    mcp_names = mcp_mgr.start_all()   # 必须先于 AgentLoop：allowed_tools 是启动时快照
    pool = BudgetPool(cfg.token_budget)   # 进程级总闸：主循环与全部子代理共用
    loop = AgentLoop(provider, prompt_version=cfg.prompt_version,
                     max_turns=cfg.max_turns, token_budget=cfg.token_budget,
                     permission_gate=gate, context_window=cfg.context_window, compact_threshold=cfg.compact_threshold,
                     keep_recent=cfg.keep_recent, journal=journal, budget_pool=pool,
                     allowed_tools={d.name for d in get_tool_defs()} - TEAM_TOOL_NAMES,
                     )   # 团队工具仅 /team leader 语境放开
    registry = SubagentRegistry()   # 子代理登记簿：Ctrl+T（任务中）/ /agents（任务间）查看
    subagent.configure(provider=provider, parent_gate=gate,
                       max_turns=cfg.subagent_max_turns, allow_bash=cfg.subagent_allow_bash,
                       token_slice=cfg.subagent_token_slice, budget_pool=pool, journal=journal,
                       max_parallel=cfg.subagent_max_parallel, registry=registry)
    team.configure(journal=journal, max_workers=cfg.team_max_workers)
    if docker_exec:
        loop.system += _env_note(docker=True)
    else:
        loop.system += _env_note(docker=False)
    print(f"[agent] provider={cfg.provider} model={cfg.model} 权限={gate.mode.value}（/help 查看命令）")
    print(f"[agent] 工作区={get_root()}（Write/Edit 仅限此目录内）")
    if docker_exec:
        print(f"[agent] 沙箱=docker（{cfg.sandbox_image}，trusted={cfg.sandbox_trusted}）"
              f"容器 {docker_exec.name}，退出时自动清理")
    print(f"[agent] 子代理=启用（只读，max_turns={cfg.subagent_max_turns}，"
          f"allow_bash={str(cfg.subagent_allow_bash).lower()}，"
          f"并发≤{cfg.subagent_max_parallel}；任务中 Ctrl+T / 任务间 /agents 查看输出）")
    if mcp_names:
        print(f"[agent] MCP={mcp_mgr.summary()}（工具以 mcp__ 前缀注入，权限门逐次询问）")
    print(f"[agent] 本次会话 {sid}")
    _hint_resume()
    try:
        while True:
            try:
                user_input = prompt("> ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\n[agent] 再见")
                return
            if not user_input:
                continue
            if user_input in ("/exit", "/quit"):
                return
            if user_input == "/help":
                print("[命令] /help 命令列表 · /stats 会话统计 · /resume [编号|ID前缀] 恢复会话\n"
                      "        /permission [default/acceptEdits/bypass] 查看/切换权限 · /exit 退出\n"
                      "        /agents [编号] 子代理列表/查看其输出（任务中可 Ctrl+T）\n"
                      "        /team <目标> 团队模式（leader 派 worker 并发干活） · /tasks 任务板与收件箱")
                continue
            if user_input == "/agents":
                print(browse(registry))
                continue
            if user_input.startswith("/agents "):
                arg = user_input.split(maxsplit=1)[1].strip()
                print(browse(registry, selection=int(arg)) if arg.isdigit()
                      else "[agent] 用法：/agents [编号]")
                continue
            if user_input == "/stats":
                tools = " ".join(f"{k}×{v}" for k, v in sorted(loop.tool_calls.items())) or "无"
                print(f"[stats] 本次进程：任务 {loop.tasks_total} 个 · "
                      f"工具 {sum(loop.tool_calls.values())} 次（{tools}）")
                print(f"        压缩 {loop.compact_count} 次 · "
                      f"累计 ↑{loop.total_prompt_tokens} ↓{loop.total_completion_tokens} tokens")
                continue
            if user_input == "/resume":
                picked = _pick_session()
                if picked:
                    try:
                        sid = _apply_resume(loop, picked)   # 恢复后续写原会话
                        print(f"[agent] 已恢复会话 {sid}（{len(loop.messages)} 条，统计与权限记忆不保留）")
                    except (OSError, SessionError) as e:
                        print(f"[agent] 恢复失败: {e}")
                continue
            if user_input.startswith("/resume "):
                prefix = user_input.split(maxsplit=1)[1].strip()
                hits = [s for s in list_sessions()
                        if not s["corrupt"] and s["id"].startswith(prefix)]
                if len(hits) == 1:
                    try:
                        sid = _apply_resume(loop, hits[0])
                        print(f"[agent] 已恢复会话 {sid}（{len(loop.messages)} 条）")
                    except (OSError, SessionError) as e:
                        print(f"[agent] 恢复失败: {e}")
                elif not hits:
                    print("[agent] 没有匹配的会话")
                else:
                    print(f"[agent] 前缀匹配 {len(hits)} 个会话，请用 /resume 编号选择")
                continue
            if user_input == "/permission":
                print(f"当前权限模式: {gate.mode.value}（可选 default/acceptEdits/bypass）")
                continue
            if user_input.startswith("/permission "):
                try:
                    gate.set_mode(user_input.split(maxsplit=1)[1].strip())
                    print(f"[agent] 权限模式已切换: {gate.mode.value}")
                except ValueError:
                    print("[agent] 未知模式，可选 default/acceptEdits/bypass")
                continue
            if user_input == "/tasks":
                board = team.get_board()
                print(board.summary())
                print(board.inbox_summary())
                continue
            if user_input.startswith("/team "):
                goal = user_input.split(maxsplit=1)[1].strip()
                if goal:
                    saved_tools, saved_system = loop.allowed_tools, loop.system
                    loop.allowed_tools = None            # leader 可见全部（含 Spawn/任务板）
                    loop.system = loop.system + team.leader_system_note()
                    try:
                        _run_task(loop, registry, goal)
                    finally:
                        loop.allowed_tools, loop.system = saved_tools, saved_system
                    if cfg.save_session:
                        try:
                            save_session(sid, loop.messages)
                        except OSError as e:
                            print(f"[agent] ⚠ 会话保存失败: {e}")
                continue
            _run_task(loop, registry, user_input)
            if cfg.save_session:  # 正常/打断/报错三种结局都保存
                try:
                    save_session(sid, loop.messages)
                except OSError as e:
                    print(f"[agent] ⚠ 会话保存失败: {e}")
    finally:
        mcp_mgr.stop_all()   # 幂等：关会话/杀子进程/停 loop 线程/摘除 mcp__ 工具
        if docker_exec:
            docker_exec.stop()


if __name__ == '__main__':
    start_loop()
