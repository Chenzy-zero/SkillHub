# SkillHub 安全管理项目

公司内网 Agent Skill / `SKILL.md` 资产的发现、版本识别、安全扫描、审核与 SkillHub 纳管项目。

## 当前阶段目标

当前第一阶段只聚焦 **已经进入公司 Gerrit 代码仓库的 Skill**，暂不把公网 Skill、本地个人 Skill、Runtime 强制可信源等问题纳入首版建设范围。

1. 以 `SKILL.md` 作为 Skill 识别锚点，其所在目录作为 `Skill Root`，整个目录作为 `Skill Package`。
2. 在 Gerrit 服务端统一触发 Skill 检查，对历史 Skill 做 Baseline 全量盘点，对后续提交做增量识别。
3. 以“仓库 + Skill 路径 + Skill 名称”识别一个 `Skill Source`；不同来源先分别登记，后续再关联到同一个逻辑 Skill。
4. 同一来源的不同 commit/revision 均保留为独立 `Source Revision`，用于 Git 来源追溯。
5. 对完整 Skill Package 计算 SHA-256 `skill_digest`，将“Git 来源版本”和“安全内容版本”分离。
6. 自动扫描由 Gerrit 服务端流程或定时任务触发，CM 依据扫描结果完成治理审核；高风险、异常或策略例外可升级人工安全复核。
7. 审核通过后的 Skill 同步/注册到公司 iflytek SkillHub；如 SkillHub 支持 Draft，可提前登记，但未通过公司策略前不得正式发布。

## 核心版本模型

```text
Canonical Skill（逻辑 Skill）
        │
        ├── Skill Source A（repo + path + name）
        │       ├── Revision 1（commit A） -> Digest X
        │       ├── Revision 2（commit B） -> Digest Y
        │       └── Revision 3（commit C） -> Digest Y
        │
        └── Skill Source B（另一个引用来源）
                └── Revision 1（commit D） -> Digest Y
```

> **Commit/Revision 是来源版本，Digest 是内容版本；安全扫描和审核绑定内容版本，Git 追溯绑定来源版本。**

不同 Source 不物理合并删除，而是通过 `Canonical Skill` 建立关联，从而保留多个引用来源及完整历史证据链。

## 当前技术路线

- **SkillHub**：当前选择自行搭建 `iflytek/skillhub` 进行验证和公司内网适配。
- **Gerrit 发现**：使用服务端 Hook / Event / Plugin 统一触发，不依赖开发者本地 Hook。
- **资产识别**：`SKILL.md` 是边界锚点，但 Skill Root 内任意受管控文件变化都需要识别。
- **内容版本**：SHA-256 `skill_digest`，不使用 MD5 作为安全完整性标识。
- **安全扫描**：支持 Gerrit 发现后自动扫描、定时批量扫描以及 SkillHub 内置扫描；公司侧保留统一审核策略和结果记录。
- **SkillHub 纳管**：审核通过后进入正式发布；若平台支持 Draft，可先同步为未发布资产。

## 仓库结构

```text
.
├── AGENTS.md
├── README.md
└── docs/
    ├── 01-open-source-skillhub-evaluation.md
    ├── 02-skill-security-management-strategy.md
    ├── 03-gerrit-skill-discovery-and-review-design.md
    ├── 04-requirements.md
    ├── 05-task-breakdown.md
    ├── 06-data-model.md
    ├── 07-rollout-plan.md
    └── 08-current-flow-reproduction.md
```

## 当前阶段

当前处于 **策略 v0.2 固化 + iflytek SkillHub 搭建 + Gerrit 发现链路设计** 阶段。

详细项目上下文、设计决策、需求与任务拆分见 [AGENTS.md](./AGENTS.md)。
