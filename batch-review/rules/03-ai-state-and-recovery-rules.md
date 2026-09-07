# AI 调度、状态与恢复规则

## 1. AI 审查边界

AI 只读取一个冻结的 Skill Package、最小任务元数据和统一结果 Schema，并只写入该任务的
`expected_result` JSON。AI 不得：

- 读取静态扫描报告、批次报告、历史结果、Manifest 或其他 Skill；
- 访问 Skill Root 之外的业务文件；
- 执行、导入、编译、安装或渲染 Skill；
- 访问网络、Git、包源、MCP 或外部工具；
- 修改批次状态、合并结果、生成报告或清理工作区。

AI 审查规则统一位于 `skills/skill-security-review/`。`.agents/`、`.codex/` 和 `.claude/`
只负责客户端发现和隔离调度。

## 2. 队列与并发

- 静态扫描完成后，脚本为当前仓库生成精简 AI 队列。
- 父会话只能读取队列元数据，不能读取 handoff、Skill 内容或扫描证据。
- 每个队列项启动全新独立 Agent，只传递自己的 handoff，不复用其他 Skill 上下文。
- 并发数量不得超过 `[concurrency].ai_reviews`。
- 所有结果落盘后由脚本统一执行 Schema 校验、导入和合并。
- 缺少结果、Schema 失败、摘要不完整或 Agent 不可用时停止，不跳过，也不由父会话代审。

## 3. 状态与恢复

状态文件是推进批次的唯一依据。终端输出、文件时间和人工口头确认不能替代状态记录。

- Skill 至少区分 `PENDING`、`WAITING_FOR_AI`、`READY_TO_ADVANCE`、`COMPLETE` 和明确失败状态。
- 当前仓库所有 Skill 结果持久化后才能清理并进入下一个仓库。
- 中断后先运行 `status`、`$ask-cc` 或 `/ask-cc`，再按当前状态继续。
- 不手工修改任务 ID、Digest、Revision、队列或结果字段。
- 配置、CSV、策略哈希、扫描器版本、输出目录或运行环境与批次记录不一致时停止并保留证据。
- 清理失败时保留工作区，修复权限后由受信脚本重试；不得递归删除宽泛目录。

初始化、真实配置和扫描器安装需要人工确认。`auto-skill-review` 只在环境通过检查后自动推进；
遇到 `INITIALIZE`、`EDIT_CONFIG`、`INSTALL_SCANNERS` 或其他真实阻塞时，只报告唯一下一步。
