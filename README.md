# 终端编码 Agent

类 Claude Code 的命令行编码助手。核心思想：**控制流交给模型**——用户输入目标，模型通过工具调用循环（读文件 → 改代码 → 跑命令 → 看结果 → 继续）自主完成任务，宿主负责执行、守门与兜底。

## 功能特性

- **Agentic Loop**：`输入 → LLM → 回复或 tool_call → 执行 → 结果回灌 → 再调 LLM`，直到任务完成；单任务步数与 token 预算双上限防失控
- **双协议接入**：Anthropic 原生协议（MiniMax 等）与 OpenAI 兼容协议（GLM / DeepSeek / Kimi / vLLM 等），config.yaml 一键切换
- **六个核心工具**：Bash / Read / Write / Edit / Grep / Glob，JSON Schema 结构化调用，错误回灌模型自愈
- **流式输出**：回答逐段实时上屏；流式重试、跨 chunk 工具参数拼接、服务端不支持时自动降级非流式
- **上下文管理**：真实 token 锚点 + 增量估算，接近窗口上限自动摘要压缩（保 Anthropic tool_use 配对），摘要失败降级截断
- **权限门控**：default / acceptEdits / bypass 三模式，危险命令模式命中强制询问，会话级总是允许/拒绝记忆
- **工作区硬边界**：Write/Edit 只允许写工作区内（`..` 逃逸、异盘、symlink 外指一律拒绝，bypass 不豁免）
- **会话持久化**：任务结束自动保存，`/resume` 编号或 ID 前缀恢复历史续聊，损坏文件隔离不炸列表
- **可观测性**：JSONL 结构化日志（llm/tool/compact/权限事件）、`/stats` 任务与 token 统计
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
| `save_session` | `true` | 任务结束自动保存会话到 `.agent/sessions/` |
| `workspace_root` | `""` | 工作区根，留空 = 启动目录；Write/Edit 硬边界 |

## 使用说明

REPL 内命令：

| 命令 | 作用 |
|------|------|
| `/help` | 命令列表 |
| `/stats` | 本次进程：任务数、工具调用分布、压缩次数、token 汇总 |
| `/resume [编号\|ID前缀]` | 列出/恢复历史会话，恢复后续写原会话 |
| `/permission [模式]` | 查看或切换权限模式 |
| `/exit` | 退出（提示符处 Ctrl+C 亦可） |

**权限模式**：

- `default`：写文件、执行命令前逐次询问（y 本次 / a 会话内总是 / n 拒绝 / e 会话内总拒）
- `acceptEdits`：文件编辑免问，命令仍询问
- `bypass`：全部放行（工作区硬边界仍然生效）

**危险命令防护**：`rm -rf`、`del /s`、`format`、`git push --force`、下载内容直接进 shell 等模式命中时，即使已有会话记忆也强制重新询问（防借道绕过）。

**打断**：任务执行中 Ctrl+C 打断当前任务、历史完整保留，提示符处再 Ctrl+C 退出。

## 项目结构

```
main.py            入口 + REPL
agent/
  loop.py          Agentic 主循环（流式接入、压缩触发、预算防线）
  providers/       双协议接入（base 抽象 + anthropic/openai + 重试装饰）
  tools/           六工具 + 注册表 + workspace 路径边界
  permissions.py   权限三模式 + 危险命令模式
  context.py       token 估算 + 自动压缩（锚点校准、配对切分）
  session.py       多会话持久化（原子写、损坏隔离）
  journal.py       JSONL 事件日志
  spinner.py       等待动画（首个流式增量即停）
tests/             143 项单测，全程零网络
docs/              开发文档（PLAN.md 总纲，P1~P8 各阶段）
```

## 开发

```bash
pip install -e ".[dev]"
pytest                # 全部测试，零网络，约 10s
```

各阶段设计决策与风险记录见 `docs/PLAN.md` 与 `docs/DEV_P*.md`。

## 安全注意事项

- **`config.yaml` 内联 API key，已被 .gitignore 排除，严禁上传或 `git add -f` 强加入库**
- `.agent/`（会话与日志，含工具参数）、`tmp_demo/` 为本地运行产物，勿上传；分享日志前注意脱敏
- Write/Edit 受工作区硬边界约束，但 **Bash 仍有系统级能力**（权限门管"同意"，不管"能力"）；高危操作建议保持 `default` 模式
- 恢复会话不保留权限记忆（总是允许/拒绝集合不持久化）——安全侧取舍，宁可多问一次

## 已知局限（路线图）

- 无命令沙箱：Bash 以当前用户权限执行，真沙箱（容器 / Job Object）在远期规划
- 无 CI 门禁：测试全靠本地自觉，待决定代码托管位置后补
- 日志按日分文件、不自动轮转；无成本核算（token × 单价）
- 权限粒度为工具级（Bash 按首命令记忆），暂无前缀规则细化
