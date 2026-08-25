# SkillHub 安全管理项目

公司内网 Agent Skill / `SKILL.md` 资产的发现、版本识别、安全扫描、审核与 SkillHub 纳管项目。

## 当前阶段目标

当前第一阶段只聚焦 **已经进入公司 Gerrit 代码仓库的 Skill**，暂不把公网 Skill、本地个人 Skill、Runtime 强制可信源等问题纳入首版建设范围。

1. 以 `SKILL.md` 作为 Skill 识别锚点，其所在目录作为 `Skill Root`，整个目录作为 `Skill Package`。
2. Gerrit 日常增量治理以 **Code Review / Patchset 文件清单** 为入口，所有 Patchset 都触发检查；历史资产另做一次 Baseline 全量盘点。
3. 以“仓库 + Skill 路径 + Skill 名称”识别一个 `Skill Source`；不同来源先分别登记，后续再关联到同一个逻辑 Skill。
4. 同一来源的不同 commit/revision 均保留为独立 `Source Revision`，用于 Git 来源追溯。
5. 对受影响 Skill Root 的完整 Skill Package 计算 SHA-256 `skill_digest`，将“Git 来源版本”和“安全内容版本”分离。
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

## Gerrit 发现模式

### Baseline

首次建设时对历史仓库进行一次全量盘点：

```text
Gerrit repositories
 -> 全量查找 SKILL.md
 -> 初始化 Skill Source Inventory
```

POC：`poc/gerrit-skill-discovery/`

### Incremental

日常运行不遍历整个仓库：

```text
Gerrit Code Review / Patchset
 -> Revision Files
 -> A/M/D/R/C
 -> 新增 SKILL.md / 命中已有 Skill Root
 -> Affected Skills
 -> 仅获取受影响 Skill Root 完整内容
 -> SHA-256 Digest
```

POC：`poc/gerrit-change-discovery/`

## 当前技术路线

- **SkillHub**：当前选择自行搭建 `iflytek/skillhub` 进行验证和公司内网适配。
- **Gerrit 触发**：所有 Code Review Patchset 进入检查流程，不依赖是否属于主干分支。
- **Gerrit 识别**：日常增量使用 Gerrit Revision Files，不做每次全仓扫描。
- **资产识别**：`SKILL.md` 是边界锚点；已有 Skill Root 内任意 Git tracked 文件变化都视为该 Skill 受影响。
- **内容版本**：SHA-256 `skill_digest`，不使用 MD5 作为安全完整性标识。
- **安全扫描**：支持 Gerrit 发现后自动扫描、定时批量扫描以及 SkillHub 内置扫描；公司侧保留统一审核策略和结果记录。
- **SkillHub 纳管**：审核通过后进入正式发布；若平台支持 Draft，可先同步为未发布资产。

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
│   └── 08-current-flow-reproduction.md
└── poc/
    ├── gerrit-skill-discovery/      # Baseline 全量盘点
    └── gerrit-change-discovery/     # Code Review 增量识别
```

## 当前阶段

当前处于 **策略 v0.2 固化 + iflytek SkillHub 搭建 + Gerrit 增量发现 POC 验证** 阶段。

详细项目上下文、设计决策、需求与任务拆分见 [AGENTS.md](./AGENTS.md)。
