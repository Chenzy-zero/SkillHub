# AI Queue、Completion 与恢复规则

## 1. AI 审查边界

AI Reviewer 只读取一个冻结 Skill Package、自己的 handoff 和统一结果 Schema，并只写自己的
`expected_result` JSON。Reviewer 不得：

- 读取其他 Skill、Batch report、历史结果或 Batch evidence；
- 访问 Skill Root 之外业务文件；
- 执行、导入、编译、安装或渲染 Skill；
- 访问网络、Git、包源、MCP 或外部工具；
- 修改 Batch State、queue、dispatch lease、合并结果、生成报告或清理工作区。

Canonical 审查策略：`.agents/skills/skill-security-review/`。
`.claude/` 与 `.codex/` 仅负责客户端适配和隔离 Agent 定义。

## 2. Batch-wide Queue 与 rolling dispatch

- 所有仓库 Static Preparation 完成并生成首版 INTERIM 报告后，才暴露一个 Batch-wide AI Queue。
- Queue item 只包含 `task_id`、`handoff`、`expected_result`、`review_size_bytes` 等最小调度信息。
- 父协调层不读取 Queue 背后的 Skill/evidence 内容。
- 默认上限 `[concurrency].ai_reviews = 5`；采用 `rolling_largest_first`，大包优先。
- 每个任务启动全新 Reviewer，不复用其他 Skill 上下文。
- 可信脚本维护独立 `dispatch_session` 和 in-flight lease；结果文件出现本身不等于 lease 已释放。

## 3. Completion-driven import

任意一个 Reviewer 完成后，协调层只回传：

```text
task_id + dispatch_session
```

可信脚本针对这一项执行：

```text
expected_result existence
→ JSON Schema
→ task/revision/digest/policy expectation
→ finalize_skill
→ durable current/final result
→ current report projection refresh
→ release exactly one dispatch lease
→ allocate one next missing task when capacity is available
```

不得等待整组五个 Reviewer 全部完成才统一 import。已经 COMPLETE 的 Task 不二次 finalize。
malformed/missing result 只在对应 Task 留下诊断并保持可恢复状态，不回滚/覆盖其他已完成结果，也不取消其他合法 in-flight Reviewer。

新 coordinator session 会重新建立 dispatch lease，并逐项恢复上次中断后已经落盘但尚未收到 completion event 的 orphan results；坏 orphan 不应阻塞其他有效 orphan 的导入。

## 4. Batch State 单写与恢复

Batch State、task index、queue 和 import 结果只由可信脚本推进。父 Agent/SubAgent 不直接写 State。

恢复规则：

- `PENDING`：尚未完成 Static Preparation。
- `WAITING_FOR_AI`：Static 已完成，需要/正在等待独立 Reviewer。
- `COMPLETE`：durable final result 已成功落盘。
- 明确失败/INCOMPLETE：保留诊断，不伪装为 PASS。
- INTERIM 报告可持续刷新，但 AI/Final 未完成项必须显示 PENDING。
- 活动批次的 `manifests`/state/control plane 绝不能手工删除或编辑。
- `work/git_download` 只有在可信脚本确认相应仓库 Static durable 后才能清理。
- 配置、CSV、policy hash、workflow/queue mode 与冻结批次不一致时停止，不自动迁移运行中的旧协议。
- `repository_batch_v1` 旧批次继续旧协议；新批次固定 `batch_wide_v2`。

初始化、真实配置和扫描器安装仍需要人工授权。`auto-skill-review` 遇到 INITIALIZE / EDIT_CONFIG /
INSTALL_SCANNERS / 权限 / evidence integrity 等阻塞时停止并返回唯一下一步，不循环猜测或自行绕过。
