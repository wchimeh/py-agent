# 终端编码 Agent

[![CI](https://github.com/wchimeh/py-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/wchimeh/py-agent/actions/workflows/ci.yml)

类 Claude Code 的命令行编码助手。核心思想：**控制流交给模型**——用户输入目标，模型通过工具调用循环（读文件 → 改代码 → 跑命令 → 看结果 → 继续）自主完成任务，宿主负责执行、守门与兜底。

## 功能特性

- **Agentic Loop**：`输入 → LLM → 回复或 tool_call → 执行 → 结果回灌 → 再调 LLM`，直到任务完成；单任务步数与 token 预算双上限防失控
- **双协议接入**：Anthropic 原生协议（MiniMax 等）与 OpenAI 兼容协议（GLM / DeepSeek / Kimi / vLLM 等），config.yaml 一键切换
- **六个核心工具**：Bash / Read / Write / Edit / Grep / Glob，JSON Schema 结构化调用，错误回灌模型自愈
- **流式输出**：回答逐段实时上屏；流式重试、跨 chunk 工具参数拼接、服务端不支持时自动降级非流式
- **上下文管理**：真实 token 锚点 + 增量估算，接近窗口上限自动摘要压缩（保 Anthropic tool_use 配对），摘要失败降级截断
- **权限门控**：default / acceptEdits / bypass 三模式，危险命令模式命中强制询问，会话级总是允许/拒绝记忆
- **工作区硬边界**：Write/Edit 只允许写工作区内（`..` 逃逸、异盘、symlink 外指一律拒绝，bypass 不豁免）；docker 模式下 `/workspace/...` 容器路径自动映射到宿主工作区根
- **命令沙箱（可选）**：`sandbox.mode: docker` 时 Bash 在长驻 Linux 容器内执行（无网络、内存/CPU/PID 上限、capability 全弃仅回加 DAC_OVERRIDE），会话结束自动清理容器；`sandbox.trusted: true` 时非危险命令免询问，危险模式仍弹（工作区是 rw 挂载）
- **注入防御**：system prompt v2 将"工具读入的外部内容"明确定义为不可信数据；环境说明运行时注入（去硬编码）
- **会话持久化**：任务结束自动保存，`/resume` 编号或 ID 前缀恢复历史续聊，损坏文件隔离不炸列表
- **多 Agent 协作**：`Agent` 工具派子代理（独立上下文跑检索/取证任务只回灌结论，保护主上下文）；同回合多个子代理线程池并发（默认 ≤3）；`/team` 团队模式 leader 用 `Spawn` 派 worker 并发干活 + 任务板流转 + SendMessage 留档
- **子代理输出可观测**：transcript 全程留痕（有界），任务执行中 **Ctrl+T** 弹菜单选看任一子代理实时输出，任务间 `/agents` 回看
- **可观测性**：JSONL 结构化日志（llm/tool/compact/权限事件）、按天轮转（默认保留 30 天）、`/stats` 任务与 token 统计
- **token 估算校准**：首个真实响应后按真实/朴素比值校准（clamp 0.5~3.0），逐步贴近真实 tokenizer
- **可靠性**：指数退避+抖动重试、超时控制、Ctrl+C 打断保留会话、原子写防半写损坏

## 快速开始

环境要求：Python ≥ 3.10，Windows / Linux / macOS。

```bash
# 1. 建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\activate            # Windows（Linux/macOS: source .venv/bin/activate）
pip install -r requirements.txt   # 精确锁定版

# 2. 配置：复制模板并填入真实 key
copy config.example.yaml config.yaml    # Linux/macOS: cp
# 编辑 config.yaml：api_key 必填；model / base_url 按所选服务

# 3. 运行
python main.py
```

可选：可编辑安装后直接使用 `agent` 命令（含开发依赖）：

```bash
pip install -e ".[dev]"
agent        # 启动；pytest 跑测试
```

## 配置说明（config.yaml）

| 键 | 默认值 | 说明 |
|----|--------|------|
| `provider` | `openai` | 协议：`anthropic`（原生）或 `openai`（兼容） |
| `model` / `base_url` / `api_key` | — | 模型名 / 服务地址（必填）/ 密钥（必填） |
| `max_tokens` | `4096` | 单次输出上限，截断时会显式提示 |
| `request_timeout` | `120` | 请求超时秒数 |
| `retry_max_attempts` / `retry_base_delay` | `3` / `1.0` | 重试次数与退避基数（指数退避+抖动） |
| `max_turns` | `25` | 单任务工具往返上限，超限强制终止 |
| `token_budget` | `500000` | 单任务 token 预算，超限强制终止 |
| `permission_mode` | `default` | 权限模式，见下节 |
| `context_window` | `128000` | 模型上下文窗口，按实际模型填；`0` 关闭压缩 |
| `compact_threshold` | `0.8` | 估算占比超过即触发压缩（0~1） |
| `keep_recent` | `8` | 压缩时保留最近 N 条消息（≥2） |
| `journal` | `true` | JSONL 日志到 `.agent/logs/` |
| `journal_keep_days` | `30` | 日志保留天数，`0` = 不清理；按 `^\d{8}\.jsonl$` 删除过期文件（会话不受影响） |
| `save_session` | `true` | 任务结束自动保存会话到 `.agent/sessions/` |
| `workspace_root` | `""` | 工作区根，留空 = 启动目录；Write/Edit 硬边界 |
| `sandbox.mode` | `none` | 命令沙箱：`none`（宿主直跑）/ `docker`（容器内执行） |
| `sandbox.image` | `python:3.12-slim` | docker 模式使用的镜像 |
| `sandbox.trusted` | `false` | docker 模式下，非危险 Bash 命令是否免审批（危险模式仍询问） |
| `sandbox.memory` / `sandbox.cpus` | `2g` / `2.0` | docker 容器内存与 CPU 上限 |
| `subagent.max_turns` | `15` | 子代理单次任务回合上限 |
| `subagent.allow_bash` | `false` | 子代理可用 Bash（仍受主会话"总是允许"记忆约束，非交互不询问） |
| `subagent.token_slice` | `100000` | 子代理单体 token 硬限（与主会话共用进程级总闸 `token_budget`） |
| `subagent.max_parallel` | `3` | 同回合子代理并发上限 |
| `team.max_workers` | `3` | `/team` 模式同时在跑 worker 上限（与 max_parallel 取小生效） |
| `prompt_version` | 最新版 | pin 系统提示词版本（`agent/prompts/system_v{N}.md` 文件即版本；当前 v3 默认含子代理指引） |

## 使用说明

REPL 内命令：

| 命令 | 作用 |
|------|------|
| `/help` | 命令列表 |
| `/stats` | 本次进程：任务数、工具调用分布、压缩次数、token 汇总 |
| `/resume [编号\|ID前缀]` | 列出/恢复历史会话，恢复后续写原会话 |
| `/permission [模式]` | 查看或切换权限模式 |
| `/agents [编号]` | 子代理列表 / 查看某子代理输出尾部（任务执行中亦可 **Ctrl+T** 随时查看） |
| `/team <目标>` | 团队模式：leader 拆解目标用 Spawn 派 worker 并发干活，收报告汇总 |
| `/tasks` | 查看任务板状态与 leader 收件箱 |
| `/exit` | 退出（提示符处 Ctrl+C 亦可） |

**权限模式**：

- `default`：写文件、执行命令前逐次询问（y 本次 / a 会话内总是 / n 拒绝 / e 会话内总拒）
- `acceptEdits`：文件编辑免问，命令仍询问
- `bypass`：全部放行（工作区硬边界仍然生效）

**危险命令防护**：`rm -rf`、`del /s`、`format`、`git push --force`、下载内容直接进 shell 等模式命中时，即使已有会话记忆也强制重新询问（防借道绕过）。

**docker 沙箱**：Bash 在容器内执行（无网络/资源上限），退出时自动清理。`sandbox.trusted: true` 时普通命令免询问，危险命令仍询问——因为工作区是 rw 挂载，`rm -rf /workspace` 真能删宿主文件。

**打断**：任务执行中 Ctrl+C 打断当前任务、历史完整保留，提示符处再 Ctrl+C 退出。

**子代理与团队**：大范围检索/证据收集类任务值得 `Agent` 拆派——子代理独立上下文跑完只回灌结论，默认只读（Write/Edit 无权，Bash 需显式开启且仅限主会话已"总是允许"的命令前缀），不能再派生子代理（防递归）。同回合多个 Agent 调用自动并发。`/team <目标>` 进入 leader 模式：Spawn 派 worker（带任务板工具 TaskCreate/TaskList/TaskUpdate/SendMessage）并发干活、跑完即回收（Spawn 返回自动把任务置 completed，不依赖模型自觉流转），leader 汇总各报告给用户。子代理与主会话共用进程级 token 总闸（`token_budget`），池尽全部终止。

## 项目结构

```
main.py            入口 + REPL（含沙箱装配与环境说明注入）
.github/
  workflows/ci.yml CI 门禁（ruff + 测试矩阵 + wheel 安装 + 覆盖率门禁）
  workflows/release.yml tag v* 触发构建并发布 GitHub Release
agent/
  loop.py          Agentic 主循环（流式接入、压缩触发、预算防线、子代理并发分组）
  providers/       双协议接入（base 抽象 + anthropic/openai + 重试装饰）
  tools/           工具集 + 注册表 + workspace 路径边界（docker /workspace 自动映射）
  tools/subagent.py  Agent 工具 + spawn 装配（子代理循环/TranscriptSink/并发常量）
  tools/team.py    任务板四工具 + SpawnTool 派 worker（/team 团队编排）
  budget.py        进程级 token 预算池（主会话与全部子代理共用，线程安全）
  viewer.py        子代理登记簿 + Ctrl+T 查看器 + /agents 渲染
  sandbox.py       命令沙箱（LocalExecutor + DockerExecutor + 探测 + trusted 判定）
  permissions.py   权限三模式 + SubGate 非交互只读门 + 危险命令模式
  context.py       token 估算 + 首次响应后按模型校准（ratio clamp）
  session.py       多会话持久化（原子写、损坏隔离）
  journal.py       JSONL 事件日志 + 按天轮转（keep_days，线程锁）
  prompts/         system prompt 文件即版本（v1/v2 + v3 默认含子代理指引）
  spinner.py       等待动画（首个流式增量即停）
tests/             304 项单测 + 4 docker 集成 skipif，全程零网络
CHANGELOG.md       版本历史（Keep-a-Changelog）
```

## 开发

```bash
pip install -e ".[dev]"
pytest                # 全部测试，零网络，约 10s
ruff check .          # lint（版本锁定 0.16.7，与 CI 一致）
```

真实模型冒烟（产生 API 费用，显式运行才会花钱）：

```bash
python scripts/smoke.py            # 10 用例全量；--list 只看清单；--only S1,S3 选择执行；S8 视 docker 配置自动 SKIP
```

CI（GitHub Actions）：push / PR 自动跑 ruff lint + 测试矩阵（ubuntu × Python 3.10~3.13、Windows/macOS × 3.13）+ wheel 构建与安装态验证 + 覆盖率门禁（≥85% branch）。打 `v*` tag 自动构建并发布 GitHub Release 附 wheel/sdist（不发 PyPI，凭据需手动 `twine upload`）。

各阶段设计决策与风险记录为本地开发文档（`docs/PLAN.md`、`docs/DEV_P*.md`，**不入库**，克隆者不可见）。完整版本历史见 [CHANGELOG.md](CHANGELOG.md)。

## 安全注意事项

- **`config.yaml` 内联 API key，已被 .gitignore 排除，严禁上传或 `git add -f` 强加入库**
- `.agent/`（会话与日志，含工具参数）、`tmp_demo/` 为本地运行产物，勿上传；分享日志前注意脱敏
- Write/Edit 受工作区硬边界约束，但 **Bash 仍有系统级能力**（权限门管"同意"，不管"能力"）；高危操作建议保持 `default` 模式
- 恢复会话不保留权限记忆（总是允许/拒绝集合不持久化）——安全侧取舍，宁可多问一次

## 已知局限（路线图）

- docker exec 超时后容器内残留进程可能存活，由 `--pids-limit 256` 与会话结束销毁容器兜底
- 工作区是 rw 挂载：容器内 `rm -rf /workspace` 真删宿主文件——所以 docker 模式下危险命令仍询问（已在设计文档声明）
- 注入防御是 prompt 级"软防御"，拦不住铁了心配合注入的模型；结构性防御（内容标记/工具结果隔离）未做
- 权限粒度为工具级（Bash 按首命令记忆），暂无前缀规则细化
- worker 回合内包干：`/team` 的 worker 不跨回合存活、无 peer 互发（复杂协作 = leader 分多回合反复 Spawn）；worker 未跑完时 leader 无法插入动作（同回合屏障语义）
- 无成本核算（token × 单价）；macOS 路径未真机验证（CI 含其测试矩阵但非真机）
