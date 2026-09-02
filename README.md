# SkillHub 安全管理项目

公司内网 Agent Skill / `SKILL.md` 资产的发现、版本识别、安全扫描、审核与 SkillHub 纳管项目。

## 当前阶段目标

当前第一阶段只聚焦 **已经进入公司 Gerrit 代码仓库的 Skill**，暂不把公网 Skill、本地个人 Skill、Runtime 强制可信源等问题纳入首版建设范围。

1. 以 `SKILL.md` 作为 Skill 识别锚点，其所在目录作为 `Skill Root`，整个目录作为 `Skill Package`。
2. 历史资产通过 Baseline 全量盘点初始化；日常增量治理使用 Gerrit 服务端 `ref-update` 作为变更触发入口。
3. 当前正式实现以 `release/hooks/ref-update` 为基础：在目标 Ref 更新过程中识别新增/变化的 Skill，写入 Skill 台账和历史记录，并将新版本安全审查状态重置为待审。
4. 第一阶段以“仓库 + 分支 + Skill 路径 + Skill 名称”管理一个 Skill 实例，并使用 Commit ID 做来源版本追溯。
5. 现阶段 MySQL 使用 `skill_summary` 保存最新状态、`skill_history` 保存历史版本；后续可再引入 Digest 等内容版本模型。
6. 安全治理目标是形成“发现 → 扫描 → 审核 → SkillHub 发布 → 持续监控 → 告警/下架 → 修复恢复”的完整闭环。
7. Gerrit 负责可信来源，安全体系负责判断内容是否可信，SkillHub 作为审核通过 Skill 的统一可信分发入口。

## 核心治理原则

> **Gerrit 管来源，安全体系管可信，SkillHub 管分发；任何公司正式 Skill 都应做到来源可追溯、版本可识别、风险可判断、发布可控制、异常可下架、过程可审计。**

## Gerrit 发现模式

### Baseline

首次建设时对历史仓库进行一次全量盘点：

```text
Gerrit repositories
 -> 全量查找 SKILL.md
 -> 初始化 Skill Inventory / skill_summary
```

历史盘点 POC：`poc/gerrit-skill-discovery/`

### Incremental / ref-update

日常增量治理基于 Gerrit 服务端 `ref-update`：

```text
Gerrit Code Review
 -> Submit
 -> refs/heads/* 准备更新
 -> ref-update
 -> 识别新增/变化 Skill
 -> skill_summary / skill_history
 -> security_reviewed = 否
 -> 安全扫描 / 审核
 -> SkillHub 发布
```

当前正式实现：`release/hooks/ref-update`

## 当前技术路线

- **Gerrit 触发**：以现有 `ref-update` 同步 Hook 作为 Skill 变更检出入口。
- **Baseline**：上线前对历史 Skill 做一次全量资产初始化，日常由 `ref-update` 增量维护。
- **资产识别**：以 `SKILL.md` 为锚点，已知 Skill Root 内文件变化视为该 Skill 发生变化。
- **版本追溯**：第一阶段使用 Gerrit/Git Commit ID 作为来源版本；后续可增加 Skill Package Digest。
- **数据库**：当前正式实现使用 MySQL 的 `skill_summary` 与 `skill_history` 维护 Skill 当前状态和历史版本。
- **安全审查**：Skill 发生变化后重新进入待审状态，后续接入自动 Scanner + CM/Security 人工复核与例外管理。
- **SkillHub**：作为公司审核后 Skill 的统一可信分发入口；新版本未完成审批前，不替换上一已批准版本。
- **持续治理**：后续补齐周期复审、风险告警、异常下架、修复恢复和审计追溯。

## 仓库结构

```text
.
├── AGENTS.md
├── README.md
├── docs/
│   ├── 01-open-source-skillhub-evaluation.md
│   ├── 02-skill-security-management-strategy.md
│   ├── 03-gerrit-skill-discovery-and-review-design.md
│   ├── 04-requirements.md
│   ├── 05-task-breakdown.md
│   ├── 06-data-model.md
│   ├── 07-rollout-plan.md
│   ├── 08-current-flow-reproduction.md
│   ├── 09-complete-user-guide.md
│   ├── 10-skill-security-governance-strategy.md
│   ├── 11-final-skill-security-management-framework.md
│   ├── 12-skill-security-implementation-plan.md
│   ├── 13-skill-batch-security-review-and-scoring-design.md
│   ├── 14-skill-batch-review-implementation-tasks.md
│   ├── 15-skill-batch-review-script-user-guide.md
│   └── 16-skill-batch-review-quick-start.md
├── batch-review/                       # 存量 Skill 批量审查程序
│   ├── config/                         # 脱敏配置样例
│   ├── examples/                       # CSV 样例
│   ├── src/skill_batch_review/         # Python 实现
│   └── tests/                          # 本地单元测试
├── .claude/
│   └── skills/
│       └── skill-security-review/      # Claude Code 项目级只读 AI 审查入口
├── poc/
│   ├── gerrit-skill-discovery/
│   └── gerrit-change-discovery/
└── release/
    └── hooks/
        └── ref-update               # 当前正式实现入口
```

## 重点文档

- [Skill 安全管理方案](./docs/11-final-skill-security-management-framework.md)（当前正式方案）
- [Skill 安全管理建设规划](./docs/12-skill-security-implementation-plan.md)（当前实施规划）
- [Skill 批量安全审查与质量评分设计](./docs/13-skill-batch-security-review-and-scoring-design.md)（存量 Skill 批量审查设计）
- [Skill 批量安全审查实施任务分解](./docs/14-skill-batch-review-implementation-tasks.md)（T00–T53 实施清单）
- [Skill 批量安全审查脚本详细使用说明](./docs/15-skill-batch-review-script-user-guide.md)（配置、执行、AI 审查、报告、清理与排障）
- [Skill 批量安全审查快速使用说明](./docs/16-skill-batch-review-quick-start.md)（待填写配置、一键启动与 Skill 触发指令）
- [Skill 安全管理策略](./docs/10-skill-security-governance-strategy.md)
- [完整使用说明](./docs/09-complete-user-guide.md)
- [Skill 安全管理策略](./docs/02-skill-security-management-strategy.md)
- [Gerrit Skill 发现与审核设计](./docs/03-gerrit-skill-discovery-and-review-design.md)

Claude Code 项目级安全审查入口位于
`.claude/skills/skill-security-review/`，可在本项目上下文中调用
`/skill-security-review`。该 Skill 是公司维护的审查流程，参考了
UseAI-pro 的 `skill-vetter` 和 `skill-auditor`，但不是上游 Skill 的原样副本。

GitHub 联调可使用 `batch-review/tools/discover_git_skills.py` 从固定 Revision 生成 CSV，
并以 `batch-review/config/review.github.example.toml` 作为可切换到正式 Gerrit 的配置模板。
完成批次后会同时生成机器可读结果和离线 HTML 管理报告。

## 当前阶段

当前处于 **基于 `ref-update` 的 Skill 资产台账落地 + 存量 Skill 批量安全审查程序实现与内网联调准备** 阶段。批量程序已完成本地模拟验收，尚未对公司真实 Skill 执行审查。

详细项目上下文、设计决策、需求与任务拆分见 [AGENTS.md](./AGENTS.md)。
