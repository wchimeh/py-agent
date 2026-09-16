# @File:     smoke.py
# @DateTime: 2026/09/16
"""真实模型冒烟测试（P12/P16）：显式运行才产生 API 费用，pytest 不会收集本文件。

用法：
  python scripts/smoke.py             # 全量 7 用例（真实 API 调用；S8 视沙箱配置自动跳过）
  python scripts/smoke.py --list      # 只列出用例，零成本
  python scripts/smoke.py --only S1,S3
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import load_config
from agent.journal import Journal
from agent.loop import AgentLoop
from agent.permissions import PermissionGate
from agent.providers import create_provider
from agent.providers.base import StopReason, ToolResultMessage, UserMessage
from agent.tools.workspace import set_root

MAX_TURNS = 8
TOKEN_BUDGET = 50000


def new_loop(cfg, **overrides):
    defaults = dict(max_turns=MAX_TURNS, token_budget=TOKEN_BUDGET,
                    permission_gate=PermissionGate("bypass"),
                    journal=Journal.daily() if cfg.journal else None)
    defaults.update(overrides)
    return AgentLoop(create_provider(cfg), prompt_version=cfg.prompt_version, **defaults)


# ---------- S1 流式长回答 ----------

def s1_stream(cfg):
    seen = []
    t0 = time.monotonic()
    resp = create_provider(cfg).chat_stream(
        [UserMessage(content="用 300 字左右分点介绍 Python 的 GIL 是什么。")],
        tools=None, system="你是简洁的技术讲解员。", on_text=seen.append)
    joined = "".join(seen)
    deltas = len(seen)
    ok_concat = bool(resp.text) and resp.text == joined.strip()   # P15 strip 归一后的新不变量
    passed = (deltas >= 3 and ok_concat
              and resp.stop_reason is StopReason.END_TURN
              and resp.prompt_tokens > 0 and resp.completion_tokens > 0)
    note = (f"增量 {deltas} 段 · 拼接一致={ok_concat} · stop={resp.stop_reason.value} · "
            f"↑{resp.prompt_tokens} ↓{resp.completion_tokens} · {time.monotonic() - t0:.1f}s")
    return passed, note


# ---------- S2 工具往返 ----------

def s2_tool_roundtrip(cfg):
    with tempfile.TemporaryDirectory() as ws:
        set_root(ws)
        target = os.path.join(ws, "smoke.txt")
        loop = new_loop(cfg)
        t0 = time.monotonic()
        result = loop.run(
            f"第一步：用 Write 工具创建文件，file_path 必须逐字符使用这个路径：{target}，"
            f"content 为 SMOKE-OK-2026。第二步：用 Read 工具读回该文件。"
            f"最后告诉我文件内容。")
        content = ""
        if os.path.exists(target):
            with open(target, encoding="utf-8") as f:
                content = f.read()
        passed = (content == "SMOKE-OK-2026" and "SMOKE-OK-2026" in result
                  and loop.tool_calls.get("Write", 0) >= 1
                  and loop.tool_calls.get("Read", 0) >= 1)
        note = (f"Write×{loop.tool_calls.get('Write', 0)} Read×{loop.tool_calls.get('Read', 0)} · "
                f"文件正确={content == 'SMOKE-OK-2026'} · 回答提及={'SMOKE-OK-2026' in result} · "
                f"{time.monotonic() - t0:.1f}s")
        return passed, note


# ---------- S3 参数校验自愈（P9） ----------

def s3_validation_selfheal(cfg):
    with tempfile.TemporaryDirectory() as ws:
        set_root(ws)
        loop = new_loop(cfg)
        t0 = time.monotonic()
        result = loop.run(
            '执行 shell 命令 echo smoke-ok。要求：第一次调用 Bash 工具时，'
            '必须把 timeout 参数故意传成字符串 "soon"（不要传数字）。'
            '如果失败，读错误信息后修正参数再执行一次，最终告诉我命令的输出。')
        validation_errs = [m for m in loop.messages
                           if isinstance(m, ToolResultMessage)
                           and m.is_error and "参数校验失败" in m.content]
        passed = bool(validation_errs) and "smoke-ok" in result.lower()
        note = (f"校验错误回灌×{len(validation_errs)} · 最终输出含 smoke-ok={'smoke-ok' in result.lower()} · "
                f"Bash×{loop.tool_calls.get('Bash', 0)} · {time.monotonic() - t0:.1f}s")
        return passed, note


# ---------- S4 强制压缩（P5/P10） ----------

def s4_forced_compact(cfg):
    with tempfile.TemporaryDirectory() as ws:
        set_root(ws)
        line10 = "TENTH-LINE-MARKER"
        for name in ("smoke_a.txt", "smoke_b.txt", "smoke_c.txt"):
            lines = [f"file {name} line {i}" for i in range(1, 41)]
            lines[9] = line10 + f" {name}"
            with open(os.path.join(ws, name), "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        loop = new_loop(cfg, context_window=3000, compact_threshold=0.5)
        t0 = time.monotonic()
        result = loop.run(
            f"依次用 Read 工具读取 {os.path.join(ws, 'smoke_a.txt')}、"
            f"{os.path.join(ws, 'smoke_b.txt')}、{os.path.join(ws, 'smoke_c.txt')}，"
            f"全部读完后告诉我三个文件各自的第 10 行内容。")
        passed = loop.compact_count >= 1 and bool(result)
        note = (f"压缩×{loop.compact_count} · 任务完成={bool(result)} · "
                f"Read×{loop.tool_calls.get('Read', 0)} · {time.monotonic() - t0:.1f}s")
        return passed, note


# ---------- S5 非流式 usage ----------

def s5_chat_usage(cfg):
    t0 = time.monotonic()
    resp = create_provider(cfg).chat([UserMessage(content="只回复两个字：收到")],
                                     tools=None, system="极度简洁。")
    passed = bool(resp.text) and resp.prompt_tokens > 0 and resp.completion_tokens > 0
    note = (f"回复={resp.text!r:.30} · ↑{resp.prompt_tokens} ↓{resp.completion_tokens} · "
            f"{time.monotonic() - t0:.1f}s")
    return passed, note


# ---------- S6 think 剥离（P15 真机回归） ----------

def s6_think_stripped(cfg):
    seen = []
    t0 = time.monotonic()
    resp = create_provider(cfg).chat_stream(
        [UserMessage(content="请先逐步推理再作答：一个数加上它自己等于 14，这个数是多少？只回答数字")],
        tools=None, system="你是严谨的推理助手。", on_text=seen.append)
    joined = "".join(seen)
    leaked = "<think>" in joined or bool(resp.text and "<think>" in resp.text)
    passed = (not leaked and bool(resp.text) and "7" in resp.text
              and resp.text == joined.strip())   # P15 strip 归一后的新不变量
    note = (f"think 泄漏={leaked} · 拼接一致={resp.text == joined.strip()} · "
            f"答对={'7' in (resp.text or '')} · {time.monotonic() - t0:.1f}s")
    return passed, note


# ---------- S8 沙箱冒烟（sandbox.mode: docker；P16 M1） ----------

def s8_docker_sandbox(cfg):
    if cfg.sandbox_mode != "docker":
        return None, "SKIP：config.yaml 未配置 sandbox.mode: docker"
    from agent.sandbox import probe_docker
    if not probe_docker():
        return None, "SKIP：docker 不可用（守护进程未运行或未安装）"
    from main import _env_note, _setup_sandbox
    with tempfile.TemporaryDirectory() as ws:
        set_root(ws)   # 必须在 _setup_sandbox 之前：容器挂载点取当时的工作区
        docker_exec = _setup_sandbox(cfg, "smoke")
        try:
            loop = new_loop(cfg)
            loop.system += _env_note(docker=True)

            result = loop.run("用 Bash 执行 echo sandbox-ok，然后把输出原样告诉我。")
            a = "sandbox-ok" in result

            probe = os.path.join(ws, ".sandbox_probe.txt")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("probe-7749")
            result = loop.run("用 Bash 读取 /workspace/.sandbox_probe.txt 的内容，原样告诉我。")
            b = "probe-7749" in result

            result = loop.run("用 Bash 检查当前环境能否访问外网"
                              "（如 curl -m 5 https://example.com），只回答两个字：通 或 不通。")
            c = "不通" in result

            note = f"容器执行={a} · 工作区互通={b} · 出网被拒={c}"
            return (a and b and c), note
        finally:
            docker_exec.stop()


CASES = [
    ("S1", "流式长回答（增量/拼接/usage）", s1_stream),
    ("S2", "工具往返（Write→Read）", s2_tool_roundtrip),
    ("S3", "参数校验自愈（timeout 传坏值）", s3_validation_selfheal),
    ("S4", "强制压缩（context_window=3000）", s4_forced_compact),
    ("S5", "非流式 usage 统计", s5_chat_usage),
    ("S6", "think 剥离回归（P15）", s6_think_stripped),
    ("S8", "沙箱冒烟（容器执行/工作区互通/出网被拒）", s8_docker_sandbox),
]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    if "--list" in args:
        for name, desc, _ in CASES:
            print(f"  {name}  {desc}")
        return 0

    if not os.path.exists("config.yaml"):
        print("[smoke] 未找到 config.yaml——请在项目根目录运行（冒烟需要真实 API 配置）")
        return 2

    only = None
    if args and args[0] == "--only" and len(args) > 1:
        only = {x.strip().upper() for x in args[1].split(",")}

    cfg = load_config()
    if not cfg.api_key or "your-key" in cfg.api_key:
        print("[smoke] config.yaml 的 api_key 未填写真实 key，拒绝运行")
        return 2

    print(f"[smoke] provider={cfg.provider} model={cfg.model} "
          f"· 用例级硬顶：max_turns={MAX_TURNS} token_budget={TOKEN_BUDGET}")
    results = []
    for name, desc, fn in CASES:
        if only and name not in only:
            continue
        print(f"\n[{name}] {desc}")
        t0 = time.monotonic()
        try:
            passed, note = fn(cfg)
        except Exception as e:
            passed, note = False, f"异常 {type(e).__name__}: {e}"
        mark = "SKIP" if passed is None else ("PASS" if passed else "FAIL")
        print(f"  {mark} · {note} · 总耗时 {time.monotonic() - t0:.1f}s")
        results.append((name, passed))

    failed = [n for n, p in results if p is False]
    skipped = [n for n, p in results if p is None]
    ran = len(results) - len(skipped)
    line = f"\n[smoke] 汇总：{ran - len(failed)}/{ran} 通过"
    if skipped:
        line += f"（{len(skipped)} 跳过：{', '.join(skipped)}）"
    if failed:
        line += f"；失败：{', '.join(failed)}"
    print(line)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
