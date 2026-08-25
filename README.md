# SkillHub 安全管理项目

公司内网 Agent Skill / SKILL.md 资产的统一注册、安全审查、发布、分发与追溯治理项目。

## 项目目标

1. 建设公司内网统一 SkillHub，所有公司内输出、引用、安装和分发的 Skill 必须来自统一 SkillHub，或已在 SkillHub 完成注册与安全审查。
2. 对现有 Gerrit 仓库中的 Skill 进行自动发现、登记、变更跟踪和安全审查，形成可追溯的 Skill 资产台账。
3. 建立“自动扫描 + 人工复核 + 发布门禁 + 运行时来源校验”的纵深防御体系，而不是只依赖单次人工审查。
4. 逐步形成公司级 Skill 安全规范、审查标准、例外流程和审计证据链。

## 推荐总体方案

- **SkillHub 控制面**：优先对 `iflytek/skillhub` 与 `Nacos 3.2+ Skill Registry` 做双轨 POC，不建议第一阶段直接完全自研。
- **SCM 发现面**：Gerrit 侧使用服务端事件/插件/CI 检查发现 Skill 变更，不依赖开发者本地 Git Hook。
- **安全扫描面**：接入 Cisco AI Skill Scanner、NVIDIA SkillSpector 等可插拔扫描器，自动扫描只作为第一层门禁，保留人工安全复核。
- **可信发布面**：审批绑定到 Skill 目录内容摘要（digest），而不仅仅绑定 commit id；任何 Skill 内容变化都会使旧审批失效或进入重新评估。
- **运行时管控面**：Agent/CLI 默认只允许从内网 SkillHub 安装，外部 Skill 必须先导入隔离区、固定版本并完成审查后再上架。

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

当前处于 **策略设计 + 技术选型 + POC 准备** 阶段。

详细项目上下文、约束、推荐架构、需求与任务拆分见 [AGENTS.md](./AGENTS.md)。
