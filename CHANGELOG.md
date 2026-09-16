# Changelog

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

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
