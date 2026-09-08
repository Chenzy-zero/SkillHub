# Skill 批量安全审查项目

本目录可独立复制和执行。本文件只保留批量审查的核心边界与主要路由；详细规则位于
`rules/`，实现和排障说明位于 `docs/`。

## 1. 执行边界

- 所有批量审查输入、规则、程序、状态和输出必须位于本目录或配置明确指定的专用受控路径。
- `inventory/` 保存只读 Skill 台账；`config/` 保存配置；`.batch-review/` 保存本机状态和证据。
- `SKILL.md` 只用于定位 Skill Root；审查对象是 Skill Root 下完整的 Skill Package。
- 下载、扫描和 AI 审查默认不执行、不导入、不编译、不安装被审查内容。
- 不读取父目录或相邻项目作为扫描输入，不把秘密写入日志、报告、AI 上下文或提交。

## 2. 核心职责

- 能确定完成的工作全部由脚本执行：CSV 解析、Git 下载、文件提取、Digest、静态扫描、状态、
  结果合并、报告和清理。
- AI 只审查一个已经冻结的 Skill Package，并按统一 Schema 写入一个结果 JSON。
- 每个 Skill 使用独立 AI 上下文；父会话只接收脚本的精简派发结果并启动 Agent，不读取内容或报告。
- 当前流程按 `repo_name + branch` 分组：每个仓库下载一次，提取并完成该组全部 Skill 后再进入
  下一个仓库。
- 内容复用必须基于完整包 Digest、扫描器版本和策略版本，并为当前 `skill_id` 保留结果和来源。
- 安全结论与质量得分分开；检查不完整或安全不通过时，质量高分不能放行。
- 结果只生成本地私密候选，不自动 Commit、Push、发布或上架 SkillHub。

## 3. 标准入口

```text
首次初始化：Windows 双击 init.cmd；Linux/macOS 执行 ./init.sh
自动审查：Codex CLI 输入 $auto-skill-review；Claude Code 输入 /auto-skill-review
查看下一步：Codex CLI 输入 $ask-cc；Claude Code 输入 /ask-cc
```

初始化、真实配置和扫描器安装需要操作人员确认。正常批次推进由 `auto-skill-review` 调用受信
脚本完成；遇到权限、配置、健康检查、输入或证据问题时必须停止并给出唯一下一步。

## 4. 主要文件路由

| 需要处理的内容 | 主要文件 |
|---|---|
| 快速开始 | `docs/16-skill-batch-review-quick-start.md` |
| 完整使用和排障 | `docs/15-skill-batch-review-script-user-guide.md` |
| 批量审查设计 | `docs/13-skill-batch-security-review-and-scoring-design.md` |
| Windows Codex/Claude 执行 | `docs/21-windows-ai-client-batch-review-guide.md` |
| 工作区和输入规则 | `rules/01-workspace-and-input-rules.md` |
| 下载、扫描和结果规则 | `rules/02-review-workflow-rules.md` |
| AI 调度、状态和恢复规则 | `rules/03-ai-state-and-recovery-rules.md` |
| 统一 AI 审查策略 | `skills/skill-security-review/SKILL.md` |
| 配置模板 | `config/` |
| Skill 台账 | `inventory/` |
| 执行程序 | `tools/`、`src/` |
| 自动化测试 | `tests/` |

## 5. 修改与验证

- 只修改任务范围内的文件，保留本机配置、运行状态和用户已有改动。
- 程序修改后至少在本目录运行 `PYTHONPATH=src python -m pytest tests -q` 或等价完整测试。
- 修改 Windows 路径或入口时同步检查 `.cmd` 和 `.sh`。
- 修改统一 AI 规则时同步更新两端适配入口、Schema、测试和对应规则文件。
- 本机配置、扫描器环境、下载区和证据区保持未跟踪；若处于 Git 仓库，按仓库约定提交和推送。
