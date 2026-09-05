# batch-review 安全审查工作区规范

## 1. 适用范围

本文件只约束 `batch-review/` 下的批量 Skill 安全审查工作。进入该目录执行任务时，
本文件与仓库根目录的 `AGENTS.md` 一起生效；本文件对审查执行范围作更具体的限定。

后续安全审查的脚本、规则和执行入口都必须位于 `batch-review/` 目录树内；运行时临时区、
下载区、Skill 副本、扫描证据、AI 结果、状态和批次报告默认也应放在这里。生产环境如果
因权限或容量需要把 `[workspace]` 指向独立的受限挂载目录，必须在配置中明确指定，并且
该目录只能服务本批次安全审查。仓库外层的 `docs/`、`poc/`、`release/`、`reports/` 等
目录属于项目其他内容，不是安全扫描工作区，不应在审查过程中读取、写入或当作证据来源。

唯一例外是配置中明确指定的只读输入，例如根目录 `test/skill_summary.csv`。Codex CLI 的
`.agents/skills/`、`.codex/agents/` 与 Claude Code 的 `.claude/skills/`、`.claude/agents/`
仅作为客户端发现和隔离调度适配层；正式审查规则统一位于本目录 `skills/`。脚本可以读取
这些输入，但不得改写、覆盖或把它们当成扫描输出。
需要改变输入时，应先生成新的批次输入并记录其 SHA-256，不得在原文件上就地修复。

## 2. 工作区边界

| 目录 | 用途 | 规则 |
|---|---|---|
| `config/` | 示例配置和本机配置 | 真实凭据不得提交；本机配置使用 `.local.toml` |
| `tools/`、`src/` | 确定性程序 | 负责导入、下载、静态扫描、合并、清理和报告 |
| `packages/` | 已批准的离线安装包 | 只使用已核验来源和 SHA-256 的包 |
| `git_download/` | 当前仓库的临时归档和 Skill 提取区 | 一次只保留当前仓库；完成后按状态机清理 |
| `skills/skill-security-review/` | AI 审查规则 | 两种客户端共用的唯一策略与结果 Schema |
| 运行时配置的 `skills_root` | 永久 Skill 副本 | 只保存 `<skill_id>/<skill_name>/`，不保存 `.git` |
| `.batch-review/` | 本机状态、清单、受限证据和批次工作文件 | 不提交 Git；清理必须由受信脚本执行 |
| `tests/` | 本地测试 | 测试不得执行被审查 Skill |

`test/`（仓库根目录）里的 CSV 是输入，不是 `batch-review/` 的生成目录。正式执行时，
应通过 `batch-review/config/*.toml` 显式指定输入路径，并在批次状态中保存原文件哈希、
编码和行号。除配置中明确列出的 CSV 和规则 Skill 外，不要把外层目录内容带入审查。

## 3. 标准执行链

所有能由脚本确定完成的步骤必须由脚本完成，顺序固定为：

```text
读取并冻结 CSV
  → 校验必需字段、保留扩展字段并按仓库/分支分组
  → 对每个仓库分支下载一次无历史归档
  → 只提取清单中的全部 Skill，不保留 .git
  → 迁移到 skills/<skill_id>/<skill_name>
  → 计算完整包 SHA-256，执行同名同内容复用判断
  → 逐 Skill 运行 Cisco AI Skill Scanner 与 NVIDIA SkillSpector
  → 为需要 AI 的 Skill 写入只读 handoff 和 AI 队列
  → 由独立 Agent 审查 Skill 内容并写入约定 JSON
  → 脚本校验 AI JSON，合并静态结果和 AI 结果
  → 写入 skill_id 下的结果、批次 CSV/JSON，确认后清理临时区
  → 当前仓库完成后进入下一个仓库
```

脚本必须使用参数数组调用 Git 和扫描器，不拼接 Shell 命令；下载和扫描默认不执行、
不导入、不安装 Skill 中的任何脚本或依赖。远程访问仅限配置的只读 Git 服务和批准的
公司包源，静态扫描保持离线模式。

## 4. 脚本与 AI 的职责

### 4.1 只能由脚本完成的工作

- 配置和 CSV 编码识别、必需字段校验、扩展字段透传和批次哈希记录；
- 仓库 URL 生成、SSH 连接、版本冻结、整仓归档、Skill 路径提取和安全清理；
- Skill 目录迁移、目录/文件清单、权限和 SHA-256 计算、内容比较和结果复用；
- Cisco、SkillSpector 的静态运行、重试、超时、原始证据保存和结果规范化；
- 状态机推进、JSON Schema 校验、静态结果与 AI 结果的合并、质量分计算；
- 批次 CSV/JSON 写入、审查状态更新、幂等恢复和清理门禁。

AI 不得代替上述脚本读取 Git、拼接命令、选择版本、运行扫描器、整理报告、修改状态
或删除工作区。

### 4.2 AI 只做 Skill 内容审查

AI 的唯一业务判断是阅读一个已经冻结的 Skill Package，按照
`batch-review/skills/skill-security-review/` 的规则给出安全问题和质量评分。AI：

- 每个 Skill 使用一个独立上下文；不同 Skill 不共享对话、缓存或结论；
- 只能读取 handoff 指定的最小元数据、结果 Schema 和当前 `skill_root` 内文件；
- 只能把一个 Schema 有效的 JSON 写到该任务的 `expected_result`；
- 不读取 `package-manifest.json`、静态扫描报告、批次报告、历史 AI 结果或其他 Skill；
- 不执行、导入、编译、安装、渲染或调用 Skill 内容，不访问网络、MCP 或外部工具；
- 不把完整文件内容、秘密或扫描报告带回父会话。

父会话只负责读取精简的 AI 队列、调度 Agent 和调用受信脚本，不负责审查内容。

## 5. 一次调用完成批次

完成首次初始化、填写本机配置并通过扫描器离线健康检查后，操作人员可以任选一个项目级
AI 客户端调用：

```text
Codex CLI：$auto-skill-review
Claude Code：/auto-skill-review
```

不需要先手工运行 `review.cmd`。`auto-skill-review` 会调用 `batch-review` 的受信启动器
完成计划、仓库下载和静态扫描；到达 AI 阶段后自动创建独立 Agent，导入结果并继续下一
Skill、下一仓库，直到批次完成或遇到真实阻塞。Windows 的 `review.cmd`、Linux/macOS
的 `review.sh` 只是同一脚本入口的兼容包装，Skill 可以调用它们，但不要求操作人员在
每个阶段重新打开窗口。

首次初始化和扫描器安装仍保留人工确认，因为它们会创建本机环境、连接网络或安装包：

```text
首次：batch-review/init.cmd 或 batch-review/init.sh
确认配置和 scanner-health.json
以后：Codex CLI 直接 $auto-skill-review；Claude Code 直接 /auto-skill-review
```

如果状态为 `INITIALIZE`、`EDIT_CONFIG`、`INSTALL_SCANNERS` 或真实输入错误，AI 不得猜测
配置或自行安装；应停止并只报告唯一需要人工处理的事项。

## 6. AI 批量调度规则

脚本在当前仓库完成静态阶段后生成一个 AI 队列。队列中的每个项目都包含独立的 task ID、
handoff 路径、Skill 根目录和 expected result 路径。Codex CLI 或 Claude Code 应：

1. 只读取队列元数据，不在父会话读取 handoff 或 Skill 文件；
2. 按 `[concurrency].ai_reviews` 作为最大并发数，为每个项目启动一个全新的项目审查 Agent；
3. 给每个 Agent 只传递它自己的 handoff；禁止把多个 Skill 拼到一个上下文；
4. 等待本批次结果落盘后调用受信启动器一次性校验和导入；
5. 缺少结果、Schema 失败、摘要不完整或 Agent 不可用时停止，不跳过、不降级为父会话审查；
6. 全部任务通过后才允许脚本清理当前仓库临时目录并进入下一仓库。

并发只适用于彼此独立的 AI 内容审查；Git 下载、状态写入、结果合并和清理仍由脚本
串行控制。任何并发失败都必须留下任务状态和证据，便于安全恢复。

## 7. 安全与数据要求

- 扫描结论绑定 `source_revision`、完整包 `skill_digest`、扫描器版本和策略版本；
- 同名且完整内容一致时可以复用已经通过的结论，但必须生成当前 `skill_id` 的目录、
  结果和复用说明；时间戳不参与比较；
- 安全结论与质量得分分开保存；安全不通过或静态检查不完整时，质量高分也不得放行；
- 原始扫描输出和 AI 原始结果留在受限证据区，不复制到私密候选目录；
- 不自动 Commit、Push、发布或上架 SkillHub；候选内容只在本地私密工作区生成；
- 任何路径、符号链接、归档条目、输出目录或清理目标异常，都应阻止该任务；
- 配置、CSV、策略、Schema、扫描器版本或 AI 规则变化时，不得继续混用旧批次；
- 不在日志、报告、提交或 AI 上下文中暴露 SSH 私钥、密码、Token、环境变量值或完整秘密。

## 8. 状态与恢复

状态文件是唯一的推进依据，不以终端输出、文件时间或人工口头确认替代状态。每个
Skill 必须能落到 `PENDING`、`WAITING_FOR_AI`、`READY_TO_ADVANCE`、`COMPLETE` 或明确
的 `INCOMPLETE`/失败状态；每个仓库必须在所有 Skill 有持久化结果后才能清理。

中断恢复时：

- 先运行只读 `status` 或 `/ask-cc`；
- 只从当前批次状态和队列继续，不手工修改 `task_id`、Digest、Revision 或结果字段；
- 发现批次配置/CSV 哈希变化、旧流程状态、输出冲突或临时目录不一致时，保留证据并
  创建新批次；不得删除旧批次来“修复”；
- 清理失败时保留工作区，修复权限后由脚本重试，不手工递归删除宽泛路径。

## 9. 开发和验证

修改 `batch-review` 程序后，至少执行：

```bash
PYTHONPATH=src python -m pytest tests -q
```

涉及 Windows 路径或脚本时，同时检查 `.cmd` 和 `.sh` 的行为。测试应覆盖：扩展 CSV
字段、整仓只下载一次、仅提取 Skill、同名同内容复用、静态双扫描、AI 队列隔离、结果
持久化、失败恢复和清理边界。不得用真实生产仓库、真实凭据或不受控的目标脚本作为测试
夹具。

新增或修改安全审查规则时，应同步更新对应的项目级 Skill、Schema、测试和本文件；不要
把一次性运行记录写入规则文件。完成验证后按仓库根目录约定提交并通过 SSH 推送，生成的
本机配置、扫描器虚拟环境、下载区和证据区保持未跟踪状态。

## 10. 快速判断

遇到不确定动作时，按以下优先级处理：

```text
能由确定性脚本完成？→ 使用脚本
需要阅读 Skill 内容并做安全/质量判断？→ 使用独立 AI Agent
需要读取静态报告、修改状态、清理、安装或发布？→ 交给脚本，AI 不做
缺少权限、配置、结果或证据？→ 停止并报告，不猜测、不跳过
```
