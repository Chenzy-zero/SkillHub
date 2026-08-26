# SkillHub 安全管理项目

公司内网 Agent Skill / `SKILL.md` 资产的发现、版本识别、安全扫描、审核与 SkillHub 纳管项目。

## 当前阶段目标

当前第一阶段只聚焦 **已经进入公司 Gerrit 代码仓库的 Skill**，暂不把公网 Skill、本地个人 Skill、Runtime 强制可信源等问题纳入首版建设范围。

1. 以 `SKILL.md` 作为 Skill 识别锚点，其所在目录作为 `Skill Root`，整个目录作为 `Skill Package`。
2. 历史资产通过 Baseline 全量盘点初始化；日常增量治理使用 Gerrit Code Review / Patchset 文件清单定位受影响 Skill，不做每次全仓扫描。
3. 当前 POC 已支持在 **Gerrit Submit** 时通过 Hooks Plugin 的同步 `submit` hook 触发完整 Discovery / Digest / Database 流程；后续仍可演进为 Patchset 阶段预扫描、Submit 阶段只做门禁校验。
4. 以“仓库 + Skill 路径 + Skill 名称”识别一个 `Skill Source`；不同来源先分别登记，后续再关联到同一个逻辑 Skill。
5. 同一来源的不同 commit/revision 均保留为独立 `Source Revision`，用于 Git 来源追溯。
6. 对受影响 Skill Root 的完整 Skill Package 计算 SHA-256 `skill_digest`，将“Git 来源版本”和“安全内容版本”分离。
7. 当前事实数据支持 MySQL / SQLite，正式 POC 推荐 MySQL；JSON 保存单次分析原始证据，HTML Dashboard 用于展示。
8. 后续自动扫描结果由 CM 依据公司策略完成治理审核；高风险、异常或策略例外可升级人工安全复核。
9. 审核通过后的 Skill 同步/注册到公司 iflytek SkillHub；如 SkillHub 支持 Draft，可提前登记，但未通过公司策略前不得正式发布。

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

### Incremental / Submit Trigger

日常运行不遍历整个仓库：

```text
Gerrit Code Review
 -> 用户点击 Submit
 -> Gerrit submit hook
 -> Revision Files
 -> A/M/D/R/C
 -> 新增 SKILL.md / 命中已有 Skill Root
 -> Affected Skills
 -> 仅获取受影响 Skill Root 完整内容
 -> SHA-256 Digest
 -> MySQL / JSON / Dashboard
```

POC：`poc/gerrit-change-discovery/`

## 当前技术路线

- **SkillHub**：当前选择自行搭建 `iflytek/skillhub` 进行验证和公司内网适配。
- **Gerrit 触发**：当前 POC 使用 Submit Hook；架构仍保留后续升级为 Patchset 预扫描 + Submit 门禁的空间。
- **Gerrit 识别**：日常增量使用 Gerrit Revision Files，不做每次全仓扫描。
- **资产识别**：`SKILL.md` 是边界锚点；已有 Skill Root 内任意 Git tracked 文件变化都视为该 Skill 受影响。
- **内容版本**：SHA-256 `skill_digest`，不使用 MD5 作为安全完整性标识。
- **数据库**：当前支持 MySQL / SQLite；正式 POC 推荐 MySQL 独立库 `skillhub_security`。
- **安全扫描**：支持在 Gerrit 发现流程后接 Scanner Adapter；公司侧保留统一审核策略和结果记录。
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
│   ├── 08-current-flow-reproduction.md
│   └── 09-complete-user-guide.md
└── poc/
    ├── gerrit-skill-discovery/      # Baseline 全量盘点
    └── gerrit-change-discovery/     # 增量识别 + DB + Dashboard + Submit Hook
```

## 使用说明

完整部署、配置、Baseline、MySQL、Gerrit Submit Hook、日常操作与故障排查请查看：

- [完整使用说明](./docs/09-complete-user-guide.md)
- [增量 POC 说明](./poc/gerrit-change-discovery/README.md)
- [Gerrit Submit Hook 说明](./poc/gerrit-change-discovery/gerrit-hooks/README.md)

## 当前阶段

当前处于 **策略 v0.2 固化 + iflytek SkillHub 搭建 + Gerrit Submit 触发的增量发现/版本存档 POC 验证** 阶段。

详细项目上下文、设计决策、需求与任务拆分见 [AGENTS.md](./AGENTS.md)。
