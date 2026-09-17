# Changelog

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [1.1.0] - 2026-09-17

多 Agent 三层递进：同步子代理 → 并行子代理 → 团队编排（P17）。

### 多 Agent 协作
- **子代理（Agent 工具）**：独立上下文窗口跑完任务只回灌结论（保护主上下文）；默认只读 Read/Grep/Glob（`subagent.allow_bash` 可开且仅限主会话已"总是允许"的命令前缀）；无 Agent 工具防递归；SubGate 非交互权限门（Write/Edit 一律拒，父 bypass 不放宽）
- **并行子代理**：同回合 ≥2 个 Agent 调用线程池并发（`subagent.max_parallel` 默认 3），结果按原顺序回灌；并发期间流式文本只入 transcript、工具事件行经 console 锁带 `[sub-N]` 前缀直印；单 Agent 回合行为与同步模式完全一致
- **团队编排（/team）**：leader 用 Spawn 派 worker 并发干活（复用子代理装配 + M2 并发分组）；任务板四工具 TaskCreate/TaskList/TaskUpdate/SendMessage；worker 回合内包干（跑完即回收，无常驻线程）；Spawn 正常返回即自动置 completed（任务板状态不依赖模型自觉调用 TaskUpdate，终止/未完成如实留 in_progress）；`team.max_workers` 上限友好拒
- **进程级 token 总闸**：`BudgetPool` 主会话与全部子代理共用 `token_budget`，池尽全部终止（与单任务硬限文案区分）

### 可观测性
- **子代理输出查看**：transcript 有界留痕（2000 行/代理）；任务执行中 **Ctrl+T** 弹菜单选看任一子代理输出（查看期间后台照跑，可刷新）；任务间 `/agents [编号]` 回看；非交互终端自动降级提示
- journal 加线程锁（多线程写同文件防交错撕行）、事件带 `agent` 字段（main/sub-N/team）；spawn/task_update/message 事件

### 其他
- system prompt v3：Agent 工具使用指引（何时拆派/任务书自包含/结论引用），`prompt_version: 2` 可回退
- **/team leader 指令式说明**：执行类工作必须派 worker、leader 不亲自执行也不预先探查目录（用户敲 /team 即团队编排意图）
- **修复 Glob/Grep 默认搜索根随进程 CWD 漂移**（P2 时代潜伏 bug，真机 /team 曝光）：默认/相对路径现锚定工作区根（`workspace.resolve_readable`）——`workspace_root` 配置为异于启动目录时不再搜错树
- 环境说明（system prompt 运行时注入）补工作区根路径：模型对任务文件范围的认知不再依赖 Bash CWD 推断
- 新配置节 `subagent`（max_turns/allow_bash/token_slice/max_parallel）与 `team`（max_workers）
- 真机冒烟新增 S9（子代理拆派）/ S10（/team 团队协作）/ S11（leader 自律：自然语言目标也派工），共 10 用例
- 测试 235 → 304 项，覆盖率 90%（branch ≥85 门禁保持），ruff 零告警

## [1.0.0] - 2026-09-16

首个发布版本：类 Claude Code 的终端编码 Agent，达到工业化标准（P1~P16 全部落地）。

### 核心能力
- **Agentic Loop**：控制流交给模型的无限循环（LLM ↔ 工具往返直到任务完成）
- **双协议接入**：OpenAI 兼容（GLM/DeepSeek/Kimi/MiniMax/vLLM）与 Anthropic Claude 原生，config.yaml 一键切换
- **核心工具集**：Bash / Read / Write / Edit / Grep / Glob，JSON Schema 结构化调用，pydantic 动态校验（lax 宽松转换）
- **REPL 交互**：流式输出逐段上屏、`/help` `/stats` `/resume` `/permission` 命令族、会话持久化与恢复

### 可靠性
- 重试退避 / 请求超时 / 单任务步数与 token 预算双上限 / Ctrl+C 优雅打断（历史保留）
- 畸形工具参数（非法 JSON、类型错误）不崩循环，错误回灌模型自愈
- 流式与非流式剥离 `<think>` 思考段（跨 chunk 状态机）；坏 base_url 启动友好报错

### 安全与沙箱
- 权限三模式：default / acceptEdits / bypass，危险命令模式警示
- 工作区写硬限：Write/Edit 仅限工作区内（bypass 也不例外）
- **Docker 沙箱**（`sandbox.mode: docker`）：Bash 在长驻受限容器内执行——无网络、内存/CPU/pids 上限、丢弃全部 capability、禁止提权；`sandbox.trusted` 沙箱内非危险命令免审批（危险命令仍询问）
- system prompt v2：外部内容不可信边界声明（prompt injection 防御），环境说明运行时注入

### 上下文管理
- token 计数 + 接近窗口上限自动压缩（摘要压缩，失败降级截断）
- 真实 usage 锚点 + ratio 校准：估算按各模型 tokenizer 特性自动修正

### 可观测性与工程
- 结构化 JSONL 日志（按天轮转，默认保留 30 天）、会话统计、prompt 文件化版本管理
- CI 门禁：ruff + 六组合测试矩阵（ubuntu/windows/macos × py3.10~3.13）+ wheel 安装验证 + 覆盖率门禁（≥85%）
- 真实模型冒烟套件 `scripts/smoke.py`（S1~S8，显式运行）

[1.0.0]: https://github.com/wchimeh/py-agent/releases/tag/v1.0.0
