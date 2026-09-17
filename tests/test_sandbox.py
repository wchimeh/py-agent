# @File:     test_sandbox.py
# @DateTime: 2026/09/16
"""沙箱层单测：Executor 抽象（Local 真跑 / Docker Fake 参数形状）+ run_command 委托 + 配置校验。
格式化（stderr 合并/退出码/截断）留在 bash.py，由 test_tools.py 既有用例锁行为。"""
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent.config import load_config


def _py(code: str) -> str:
    # sys.executable 而非裸 python：Ubuntu 等裸系统只有 python3（P13 CI 教训）
    return f'"{sys.executable}" -c "{code}"'


# ---------- LocalExecutor：行为对齐原 BashTool 的 subprocess 语义 ----------

def test_local_success():
    from agent.sandbox import LocalExecutor
    rc, out, err = LocalExecutor().run("echo hello-sandbox", timeout=30)
    assert rc == 0 and "hello-sandbox" in out and err == ""


def test_local_nonzero_returns_stderr_raw():
    from agent.sandbox import LocalExecutor
    rc, out, err = LocalExecutor().run(
        _py("import sys; print('boom', file=sys.stderr); sys.exit(3)"), timeout=30)
    assert rc == 3 and out.strip() == "" and "boom" in err


def test_local_timeout_raises_timeoutexpired():
    from agent.sandbox import LocalExecutor
    with pytest.raises(subprocess.TimeoutExpired):
        LocalExecutor().run(_py("import time; time.sleep(10)"), timeout=1)


# ---------- run_command：全局执行器委托 ----------

def test_run_command_delegates_to_executor(monkeypatch):
    from agent import sandbox
    calls = {}

    class Fake:
        def run(self, command, timeout):
            calls["args"] = (command, timeout)
            return (0, "fake-out", "fake-err")

    monkeypatch.setattr(sandbox, "_EXECUTOR", Fake())
    assert sandbox.run_command("ls -la", timeout=7) == (0, "fake-out", "fake-err")
    assert calls["args"] == ("ls -la", 7)


def test_run_command_default_is_local():
    from agent.sandbox import run_command
    rc, out, _ = run_command("echo default-local", timeout=30)
    assert rc == 0 and "default-local" in out


def test_run_command_timeout_passthrough():
    from agent.sandbox import run_command
    with pytest.raises(subprocess.TimeoutExpired):
        run_command(_py("import time; time.sleep(10)"), timeout=1)


# ---------- config：sandbox 节校验与解析 ----------

def test_config_rejects_bad_sandbox_mode(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("sandbox:\n  mode: k8s\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="sandbox"):
        load_config(str(p))


def test_config_sandbox_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("", encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.sandbox_mode == "none"
    assert cfg.sandbox_image == "python:3.12-slim"
    assert cfg.sandbox_trusted is False


def test_config_sandbox_docker_parsed(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("sandbox:\n  mode: docker\n  image: python:3.11-slim\n"
                 "  trusted: true\n  memory: 1g\n  cpus: 1.5\n", encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.sandbox_mode == "docker"
    assert cfg.sandbox_image == "python:3.11-slim"
    assert cfg.sandbox_trusted is True
    assert cfg.sandbox_memory == "1g"
    assert cfg.sandbox_cpus == 1.5


# ---------- DockerExecutor：Fake docker CLI 断言参数形状（零真实 docker） ----------

class FakeDocker:
    """按子命令脚本回放：calls 记录全部 argv，供断言。"""

    def __init__(self, inspect_rc=1, run_rc=0, exec_rc=0,
                 exec_out="", exec_err="", run_err=""):
        self.calls: list[list[str]] = []
        self.inspect_rc, self.run_rc, self.exec_rc = inspect_rc, run_rc, exec_rc
        self.exec_out, self.exec_err, self.run_err = exec_out, exec_err, run_err
        self.exec_raises = None

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        sub = argv[1] if len(argv) > 1 else ""
        if sub == "inspect":
            return SimpleNamespace(returncode=self.inspect_rc, stdout="", stderr="")
        if sub == "run":
            return SimpleNamespace(returncode=self.run_rc, stdout="fake-container-id\n",
                                   stderr=self.run_err)
        if sub == "exec":
            if self.exec_raises:
                raise self.exec_raises
            return SimpleNamespace(returncode=self.exec_rc, stdout=self.exec_out,
                                   stderr=self.exec_err)
        if sub == "rm":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"未预期的 docker 子命令: {argv}")

    def argv_of(self, sub: str) -> list[str]:
        return next(c for c in self.calls if len(c) > 1 and c[1] == sub)


def make_docker(ws="/tmp/ws"):
    from agent.sandbox import DockerExecutor
    return DockerExecutor(name="agent-sandbox-t1", image="python:3.12-slim",
                          workspace_host=ws, memory="2g", cpus=2.0)


def test_docker_run_hardening_flags(monkeypatch):
    from agent import sandbox
    fake = FakeDocker(exec_out="hi\n")
    monkeypatch.setattr(sandbox.subprocess, "run", fake)
    rc, out, err = make_docker().run("echo hi", timeout=30)
    assert (rc, err) == (0, "") and "hi" in out
    argv = fake.argv_of("run")
    for flag in ("--network", "none", "--cap-drop", "ALL", "--security-opt",
                 "no-new-privileges", "--pids-limit", "256",
                 "--memory", "2g", "--cpus", "2.0"):
        assert flag in argv, f"docker run 缺少加固参数 {flag}: {argv}"
    # drop ALL 后回加 DAC_OVERRIDE：否则容器 root 无力绕过 DAC，写不了非 root 属主的工作区（CI 实证）
    assert argv[argv.index("--cap-add") + 1] == "DAC_OVERRIDE", argv
    assert argv.index("--cap-drop") < argv.index("--cap-add")       # drop 先于 add
    assert argv[argv.index("-v") + 1] == "/tmp/ws:/workspace"   # 工作区固定挂载点
    assert argv[argv.index("-w") + 1] == "/workspace"
    assert "python:3.12-slim" in argv and "sleep" in argv       # 长驻容器


def test_docker_reuses_existing_container(monkeypatch):
    from agent import sandbox
    fake = FakeDocker(inspect_rc=0)   # 同名容器已在
    monkeypatch.setattr(sandbox.subprocess, "run", fake)
    make_docker().run("echo hi", timeout=30)
    assert not any(len(c) > 1 and c[1] == "run" for c in fake.calls)   # 不重建
    assert fake.argv_of("exec")[1] == "exec"


def test_docker_exec_shape_and_timeout(monkeypatch):
    from agent import sandbox
    fake = FakeDocker()
    monkeypatch.setattr(sandbox.subprocess, "run", fake)
    d = make_docker()
    d.run("echo hi", timeout=9)
    argv = fake.argv_of("exec")
    assert argv[:2] == ["docker", "exec"]
    assert argv[argv.index("-w") + 1] == "/workspace"
    assert argv[-3:] == ["sh", "-c", "echo hi"]

    fake2 = FakeDocker()
    fake2.exec_raises = subprocess.TimeoutExpired(cmd="docker exec", timeout=1)
    monkeypatch.setattr(sandbox.subprocess, "run", fake2)
    with pytest.raises(subprocess.TimeoutExpired):
        make_docker().run("sleep 10", timeout=1)


def test_docker_exec_result_passthrough(monkeypatch):
    from agent import sandbox
    fake = FakeDocker(exec_rc=3, exec_out="", exec_err="boom")
    monkeypatch.setattr(sandbox.subprocess, "run", fake)
    rc, out, err = make_docker().run("bad-cmd", timeout=30)
    assert (rc, out, err) == (3, "", "boom")


def test_docker_start_failure_raises_friendly(monkeypatch):
    from agent import sandbox
    fake = FakeDocker(run_rc=1, run_err="Error response from daemon: pull access denied")
    monkeypatch.setattr(sandbox.subprocess, "run", fake)
    with pytest.raises(sandbox.SandboxError, match="沙箱容器启动失败"):
        make_docker().run("echo hi", timeout=30)


def test_docker_stop_rm_force(monkeypatch):
    from agent import sandbox
    fake = FakeDocker()
    monkeypatch.setattr(sandbox.subprocess, "run", fake)
    d = make_docker()
    d.run("echo hi", timeout=30)
    d.stop()
    argv = fake.argv_of("rm")
    assert argv == ["docker", "rm", "-f", "agent-sandbox-t1"]


# ---------- probe_docker + main 装配（任务 1-7） ----------

def _restore_local():
    from agent import sandbox
    from agent.tools.bash import refresh_description
    sandbox.set_executor(sandbox.LocalExecutor())
    refresh_description()


def _fake_engine(ostype: str, version_rc: int = 0):
    """按子命令回放：version 查连通性，info 查 OSType（CI 实测 Windows 引擎不支持 pids-limit）。"""
    def fake(argv, **kw):
        if len(argv) > 1 and argv[1] == "info":
            return SimpleNamespace(returncode=0, stdout=ostype, stderr="")
        return SimpleNamespace(returncode=version_rc, stdout="", stderr="x")
    return fake


def test_probe_docker_ok(monkeypatch):
    from agent import sandbox
    monkeypatch.setattr(sandbox.subprocess, "run", _fake_engine("linux"))
    assert sandbox.probe_docker() is True


def test_probe_docker_windows_engine_rejected(monkeypatch):
    from agent import sandbox
    monkeypatch.setattr(sandbox.subprocess, "run", _fake_engine("windows"))
    assert sandbox.probe_docker() is False    # Windows 容器引擎跑不了 Linux 镜像与 pids-limit


def test_probe_docker_engine_case_insensitive(monkeypatch):
    from agent import sandbox
    monkeypatch.setattr(sandbox.subprocess, "run", _fake_engine("Linux\n"))
    assert sandbox.probe_docker() is True     # docker info 输出带换行/大小写容错


def test_probe_docker_down(monkeypatch):
    from agent import sandbox
    monkeypatch.setattr(sandbox.subprocess, "run",
                        lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr="x"))
    assert sandbox.probe_docker() is False


def test_probe_docker_not_installed(monkeypatch):
    from agent import sandbox
    def boom(argv, **kw):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(sandbox.subprocess, "run", boom)
    assert sandbox.probe_docker() is False


def test_main_setup_sandbox_docker(monkeypatch):
    import main
    from agent import sandbox
    from agent.config import AgentConfig
    from agent.tools.bash import BashTool
    monkeypatch.setattr(main, "probe_docker", lambda: True)
    cfg = AgentConfig(sandbox_mode="docker", sandbox_image="img-x",
                      sandbox_trusted=True, sandbox_memory="1g", sandbox_cpus=1.5)
    try:
        executor = main._setup_sandbox(cfg, "sid9")
        assert isinstance(executor, sandbox.DockerExecutor)
        assert executor.name == "agent-sandbox-sid9"
        assert executor.image == "img-x" and executor.memory == "1g"
        assert sandbox.get_executor() is executor
        assert "容器" in BashTool.description      # 工具描述已切换
    finally:
        _restore_local()


def test_main_setup_sandbox_none_returns_none():
    import main
    from agent import sandbox
    from agent.config import AgentConfig
    assert main._setup_sandbox(AgentConfig(), "s") is None
    assert isinstance(sandbox.get_executor(), sandbox.LocalExecutor)


def test_main_setup_sandbox_no_docker_exits(monkeypatch):
    import main
    from agent.config import AgentConfig
    monkeypatch.setattr(main, "probe_docker", lambda: False)
    with pytest.raises(SystemExit, match="docker"):
        main._setup_sandbox(AgentConfig(sandbox_mode="docker"), "s")


# ---------- M2：环境说明行注入（system_v2 去硬编码，运行时拼接） ----------

def test_env_note_windows_local():
    import main
    assert "cmd.exe" in main._env_note(docker=False, nt=True)


def test_env_note_posix_local():
    import main
    assert "POSIX" in main._env_note(docker=False, nt=False)


def test_env_note_docker():
    import main
    note = main._env_note(docker=True)
    assert "/workspace" in note and "容器" in note


# ---------- 真实 docker 集成（CI ubuntu 自带 docker；本机不可用则跳过） ----------

import shutil  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

from agent.sandbox import probe_docker  # noqa: E402

DOCKER_AVAILABLE = shutil.which("docker") is not None and probe_docker()

IT_MOUNT_DIR = Path(__file__).resolve().parent.parent / "tmp_docker_it"


def _it_ws(name: str) -> Path:
    # 挂仓库内路径而非 pytest /tmp：GH 托管 runner 的 /tmp bind mount 会静默为空（CI 实证 2026-09-17）
    d = IT_MOUNT_DIR / f"{name}-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _it_cleanup() -> None:
    shutil.rmtree(IT_MOUNT_DIR, ignore_errors=True)


def _real_docker(ws):
    from agent.sandbox import DockerExecutor
    return DockerExecutor(name=f"agent-sandbox-it-{uuid.uuid4().hex[:8]}",
                          image="python:3.12-slim", workspace_host=str(ws))


@pytest.mark.skipif(not DOCKER_AVAILABLE, reason="本机无可用 docker（未安装/守护进程未运行/非 Linux 引擎）")
def test_docker_it_echo_roundtrip():
    d = _real_docker(_it_ws("echo"))
    try:
        rc, out, _ = d.run("echo hello-docker", timeout=60)
        assert rc == 0 and "hello-docker" in out
    finally:
        d.stop()
        _it_cleanup()


@pytest.mark.skipif(not DOCKER_AVAILABLE, reason="本机无可用 docker")
def test_docker_it_workspace_shared_both_ways():
    ws = _it_ws("share")
    (ws / "probe.txt").write_text("from-host", encoding="utf-8")
    d = _real_docker(ws)
    try:
        rc, out, _ = d.run("cat /workspace/probe.txt", timeout=60)
        assert rc == 0 and "from-host" in out        # 宿主 → 容器
        rc, out, _ = d.run("echo from-container > /workspace/out.txt", timeout=60)
        assert rc == 0
        assert (ws / "out.txt").read_text(encoding="utf-8").strip() == "from-container"
    finally:
        d.stop()
        _it_cleanup()


@pytest.mark.skipif(not DOCKER_AVAILABLE, reason="本机无可用 docker")
def test_docker_it_network_denied():
    d = _real_docker(_it_ws("net"))
    try:
        rc, _, _ = d.run(
            "python -c \"import urllib.request; urllib.request.urlopen('https://example.com', timeout=5)\"",
            timeout=60)
        assert rc != 0                               # --network none：出网必失败
    finally:
        d.stop()
        _it_cleanup()


@pytest.mark.skipif(not DOCKER_AVAILABLE, reason="本机无可用 docker")
def test_docker_it_stop_removes_container():
    import subprocess as sp
    d = _real_docker(_it_ws("stop"))
    d.run("echo hi", timeout=60)
    d.stop()
    _it_cleanup()
    r = sp.run(["docker", "inspect", d.name], capture_output=True, text=True, timeout=30)
    assert r.returncode != 0                        # 容器已被清理
