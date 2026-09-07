# SkillHub 安全管理项目

本文件只保留当前有效的核心规则和文件路由。详细约束见 `rules/`；方案背景、调研过程和
历史决策仅用于参考，不得覆盖本文件及当前正式方案。

## 1. 项目范围

- 当前治理对象是公司 Gerrit 仓库中以 `SKILL.md` 为识别锚点的 Skill。
- `SKILL.md` 所在目录是 `Skill Root`，该目录下全部受管控文件构成 `Skill Package`。
- 安全检查、摘要和内容版本必须覆盖整个 Skill Package，不能只检查 `SKILL.md`。
- 员工本地临时 Skill、公网直接安装和终端侧旁路检测不在当前闭环内；未纳管不代表可信。
- 进入 `batch-review/` 工作时，必须同时遵循其独立的 `AGENTS.md`。

## 2. 核心规则

1. Skill Source 使用 `repository + branch + skill_path + skill_name` 标识；不得仅凭名称合并。
2. Git Commit/Revision 表示来源版本，整个 Skill Package 的 SHA-256 `skill_digest` 表示内容版本。
3. Source Revision 和历史审核记录不可覆盖；Canonical Skill 合并只能建立关联，不能删除来源。
4. 扫描和审核结果绑定 `skill_digest + scanner_version + policy_version`；只有三者满足复用条件时
   才能复用，并保留每个来源版本的追溯关系。
5. Gerrit 发现必须覆盖新增、修改、删除、重命名、复制以及 Skill Root 内非 `SKILL.md` 文件变化。
6. 自动扫描、人工审核和 SkillHub 状态相互独立；任一工具的通过结果都不能单独代表最终结论。
7. 平台可以建立私密候选并提醒产品线，但产品线必须自行确认和上传；不得自动公开发布。
8. 默认不执行、不导入、不安装被审查 Skill 中的脚本或依赖；秘密不得进入日志、报告或提交。
9. 关键任务必须幂等，关键状态变化必须留有审计记录，任何失败不得破坏已有证据和历史结果。

## 3. 工作约束

- 先读取与任务范围最接近的规则和设计文件，再修改代码或文档。
- 保留用户已有改动，不覆盖无关文件，不使用破坏性 Git 操作。
- 方案文档只描述当前有效设计；计划单独维护，历史材料不得混入正式方案。
- 修改后按影响范围执行测试、路径检查或文档校验。
- 完成并验证仓库修改后，默认在同一轮提交并通过 SSH 推送；用户明确要求不提交或不推送时除外。

## 4. 主要文件路由

| 需要处理的内容 | 主要文件 |
|---|---|
| 当前正式管理框架 | `docs/11-final-skill-security-management-framework.md` |
| 当前建设计划 | `docs/12-skill-security-implementation-plan.md` |
| 治理对象、版本和发布细则 | `rules/01-skill-governance-rules.md` |
| 工程、安全和审计细则 | `rules/02-engineering-and-audit-rules.md` |
| Gerrit 发现实现 | `release/` |
| 批量安全审查项目 | `batch-review/` |
| 批量审查执行规则 | `batch-review/AGENTS.md` |
| 批量审查快速入口 | `batch-review/docs/16-skill-batch-review-quick-start.md` |
| 批量审查详细使用说明 | `batch-review/docs/15-skill-batch-review-script-user-guide.md` |
| Windows AI 客户端说明 | `batch-review/docs/21-windows-ai-client-batch-review-guide.md` |

`docs/01` 至 `docs/10` 以及 `poc/` 属于背景、调研或早期验证材料。需要了解来源时可以读取，
但不得把其中已失效的选择重新写入当前方案或实现。
