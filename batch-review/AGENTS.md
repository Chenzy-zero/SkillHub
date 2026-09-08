# Skill 批量安全审查项目

本目录可独立复制和执行。`AGENTS.md` 与 `README.md` 是操作和维护入口；canonical 规则位于
`.agents/rules/`，canonical AI 审查策略位于 `.agents/skills/skill-security-review/`。
`.claude/` 与 `.codex/` 只提供客户端适配/隔离 Agent 定义，不作为业务策略的第二来源。

## 1. 不可突破的执行边界

- `SKILL.md` 只用于定位 Skill Root；审查对象是该 Root 下冻结的完整 Skill Package。
- 下载、静态扫描和 AI 审查都不得执行、导入、编译、安装或渲染被审查内容。
- 每个 Skill 使用独立 Reviewer 上下文；父协调会话只接收 `task_id`、handoff、expected_result 和 completion metadata。
- SubAgent 只能写自己的 `expected_result`，不能写 Batch State、合并结果、生成报告或清理目录。
- Batch State 只有可信脚本写；配置、CSV、策略、Revision、Digest 不匹配时 fail closed。
- 不把秘密、原始 scanner/AI evidence 或绝对受限路径嵌入普通报告/父 AI 上下文。
- 不自动 Commit、Push、发布或上架候选内容。

## 2. 当前标准流程

```text
冻结配置 + CSV
→ 按 repo_name + branch 下载一次无历史归档
→ 逐仓库完成所有 Skill 的 Static Preparation
→ 每个仓库 Static 完成即清理该仓库 Runtime，不等待 AI
→ 全 Batch Static 完成后生成 INTERIM 报告
→ 生成一个 batch-wide AI Queue
→ rolling_largest_first，最多 ai_reviews 个独立 Reviewer
→ 任意 Reviewer completion event 到达即单项校验/finalize
→ 立即刷新 current/final result 和报告投影
→ 释放一个 dispatch lease 并补派下一个 Task
→ 全部 Skill resolved 后生成 FINAL 报告
```

旧的 `repository_batch_v1` 已启动批次仍按旧协议兼容续跑；新批次使用 `batch_wide_v2`，不得静默切换旧批次协议。

## 3. Runtime / State / Archive+Evidence / Reports 职责

profile 初始化默认写到 `.batch-review/<profile>/`；生产配置也可把各 root 指向独立受控目录。
物理路径第一阶段保持兼容，不迁移历史批次。

| 目录/配置键 | 责任 | 生命周期 / 删除规则 |
|---|---|---|
| `work` / `workspace.root` | Runtime：临时执行、scanner work | 仅在没有活动/恢复中的任务且可信清理已完成后可删除 |
| `git_download` | Runtime：无历史仓库归档/提取临时区 | 每仓 Static 结束后由可信脚本清理；失败恢复前不要手删 |
| `manifests` / `manifest_root` | State：Batch state、task index、AI queue、dispatch lease | 活动批次绝不能删除；键名为历史兼容，职责实际是 state/control plane |
| `skills` / `skills_root` | Archive：冻结 Skill Package + `current-result.json`/`review-result.json` | 持久审计/复用数据，不能当临时目录清理 |
| `restricted-evidence` / `evidence_root` | Evidence：RAW/SOURCE/DERIVED/NORMALIZED evidence + index | 受限持久审计数据；RAW 不进入普通报告 |
| `private-candidates` / `candidate_root` | Candidate staging | 受控发布候选，不是审计事实；按发布治理策略处理 |
| `results/<batch-id>` / `results_root` | Reports：人工报告与标准化导出 | 完成批次的人工/审计查看入口；可按组织留存策略归档 |

**唯一人工报告入口：**
`results/<batch-id>/skill-security-review-report.html`

`skill-review-results.csv/json` 是按原始 inventory 重建的 **ledger/reconciliation projection**，用于系统对账和后续自动化；
`details.csv`、`batch-summary.json`、`failures.json`、`candidates.json`、`current-review-results.*` 是 **标准化 report exports**。
二者都不是第二个人工入口，避免用 `details.csv` 代替台账更新或用 ledger CSV 代替审计工作台。

## 4. 标准入口与规则路由

```text
首次初始化：Windows init.cmd；Linux/macOS ./init.sh
自动审查：Codex CLI $auto-skill-review；Claude Code /auto-skill-review
只读状态：Codex CLI $ask-cc；Claude Code /ask-cc
```

| 内容 | Canonical 路径 |
|---|---|
| 工作区与输入规则 | `.agents/rules/01-workspace-and-input-rules.md` |
| 下载、Static、报告规则 | `.agents/rules/02-review-workflow-rules.md` |
| AI Queue、completion、恢复 | `.agents/rules/03-ai-state-and-recovery-rules.md` |
| AI 安全审查策略 | `.agents/skills/skill-security-review/SKILL.md` |
| AI 结果 Schema | `.agents/skills/skill-security-review/references/review-result.schema.json` |
| 自动调度 Skill | `.agents/skills/auto-skill-review/SKILL.md`、`.claude/skills/auto-skill-review/SKILL.md` |
| 配置模板 | `config/` |
| 台账输入 | `inventory/` |
| 确定性程序 | `tools/`、`src/` |
| 回归测试 | `tests/` |

不存在的根级 `docs/`、`rules/`、`skills/` 不得作为核心路由重新写入文档。旧本地 TOML 若仍指向
`skills/skill-security-review`，加载器只对这个已知仓库旧路径做内存兼容重定向，不改写用户配置。

## 5. 修改与验证

- 保留用户本机配置、运行状态和已有 evidence，不为重构迁移历史批次。
- 修改程序后完整执行：`python -m pytest tests -q`。
- GitHub 回归由 `.github/workflows/batch-review-regression.yml` 执行 Linux/Windows Python 矩阵。
- 修改 `.cmd` 时同步检查 `.sh`；修改 AI 协议时同步 `.agents/.claude/.codex` 对应入口与测试。
- 报告逻辑必须保持：INTERIM 不显示未完成 AI 为最终 PASS；FINAL 仅在全部 selected Skill resolved 后形成。
- Evidence 导航必须保持：RAW/SOURCE path-only；DERIVED/NORMALIZED 才允许经 root/symlink 校验后的受控相对打开。
