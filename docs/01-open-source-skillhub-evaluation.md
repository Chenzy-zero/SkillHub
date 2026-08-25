# 开源 SkillHub / Skill Registry 方案调研

> 调研日期：2026-08-25
>
> 目标：选择适合公司内网 Skill 注册、审查、发布、分发和审计治理的基础平台，而不是只找一个公开 Skill 市场。

## 1. 结论摘要

建议进入 POC 的核心候选：

1. **iflytek/skillhub**：作为“企业专用 Skill Registry”第一候选。
2. **Nacos 3.2+ Skill Registry**：作为“统一 AI Registry 控制面”第一候选。
3. **saker-ai/skillhub**：作为轻量技术参考/备选 POC。
4. **airopshq/skillshub**：主要参考 Git-native 同步、MCP 与客户端分发，不作为安全治理主平台。

暂不建议直接完全自研。应先证明现有开源项目在关键能力上确实无法满足公司要求，再决定自研范围。

---

## 2. 候选方案分析

## 2.1 iflytek/skillhub

### 定位

企业级、自托管 Agent Skill Registry，强调发布、发现、版本、命名空间、RBAC、审计、安全扫描和私有化部署。

### 当前可见能力

- Apache License 2.0；
- Web UI + REST API + CLI；
- Skill 发布、版本、标签；
- 命名空间；
- 平台/命名空间 RBAC；
- 审计日志；
- 安全 Scanner 服务；
- Docker / Kubernetes 部署；
- PostgreSQL、Redis、对象存储等企业部署组件；
- 支持本地认证、OAuth 等方式，并有较完整的工程化文档。

### 优点

- 与本项目目标最接近，少走大量“从 0 到 1”的产品开发；
- 已经把 Skill 当成正式供应链资产治理，而不只是 Git 目录；
- 安全扫描、审核、权限和审计都已有落点；
- 代码结构清晰，适合公司二次开发；
- Apache-2.0 对企业二开友好。

### 风险/待验证

- 当前仍处于 `0.x` 版本阶段，API/数据模型升级稳定性需要评估；
- SSO/LDAP/OIDC 与公司 IAM 的接入工作量；
- 扫描器能力是否足够，需要接入公司自定义 Scanner；
- 高可用、备份恢复、升级回滚、数据迁移的成熟度；
- 管理员直发、CLI、API、Web 等所有入口是否都能统一走安全策略；
- 与 Gerrit 的原生集成程度。

### POC 优先级

**高。**

---

## 2.2 Nacos 3.2+ Skill Registry

### 定位

Nacos AI Registry 中的 Skill 管理能力。Nacos 3.2.0 开始提供 Skill Registry，支持 Skill 创建、版本、审查、发布和分发。

### 当前可见能力

- Skill 生命周期：draft / reviewing / online / offline；
- 版本和标签；
- 安全发布 Pipeline；
- PUBLIC / PRIVATE 等可见性能力；
- Namespace 隔离；
- CLI / REST API / Java SDK；
- 可与 MCP Registry、Agent Registry、Prompt Registry、AgentSpec 等形成统一 AI 控制面；
- Nacos 本身拥有成熟的服务治理和企业部署经验。

### 优点

- 如果公司未来要统一管理 Skill、Prompt、MCP、Agent，平台整体性很强；
- Nacos 社区和工程成熟度高于多数新生 SkillHub 项目；
- 公司若已有 Nacos，运维、监控、权限、部署体系可能直接复用；
- Java 生态和企业系统集成方便。

### 风险/待验证

- Skill Registry 是 2026 年新增能力，功能成熟度与 Nacos 传统配置/注册中心能力不能直接等同；
- 需要验证安全 Pipeline 是否支持公司自定义扫描器、人工审批和策略版本；
- 需要验证 Gerrit 自动发现和外部 Skill 隔离导入；
- Skill 审核 UI、审计证据、diff 审查体验是否达到 CM/安全运营要求；
- 对客户端“只允许来自内部 Registry”这一运行时门禁仍需公司自行补充。

### POC 优先级

**高。**

---

## 2.3 saker-ai/skillhub

### 定位

轻量自托管 Skill/Plugin Registry，Go 单体，Git 原生版本管理。

### 可见能力

- 单二进制部署；
- SQLite / PostgreSQL；
- Git commit 作为 Skill 版本基础；
- Web UI / REST / CLI；
- GitHub/GitLab/Gitea Webhook 导入；
- Token、RBAC；
- 搜索和分发。

### 优点

- 部署简单；
- Git-native 思路与公司 Gerrit 场景契合；
- 很适合做快速原型和数据模型参考。

### 风险

- 社区规模和生产案例需进一步确认；
- 企业 SSO、审计、安全扫描、审批体系可能需要较多自研；
- 如果二开量过大，最终成本可能接近自研。

### POC 优先级

**中。**

---

## 2.4 airopshq/skillshub

### 定位

Git 仓库作为团队 Skills 单一事实源，通过 CLI/MCP 同步到 Agent。

### 价值

很适合参考：

- Git 作为源；
- Agent Skills 目录同步；
- MCP 管理接口；
- 多 Agent 客户端安装；
- Skill diff / rollback；
- 团队目录分层。

### 不足

它更像“Skill 同步/分发工具”，不是“企业安全 Registry”。

缺少本项目最关键的：

- 完整安全审核门禁；
- 组织级 RBAC/职责分离；
- 风险接受/撤销流程；
- 完整审计证据；
- 企业内网统一可信发布中心。

### POC 优先级

**低（作为客户端/同步参考较高）。**

---

## 2.5 公共目录类项目

例如：

- ComeOnOliver/skillshub
- agent-skills-hub/agent-skills-hub
- Agent-mag/skills

它们适合参考：

- 分类；
- 搜索；
- 推荐；
- 安装体验；
- 元数据；
- 社区贡献流程。

但不应直接作为公司 Skill 安全治理底座。

---

## 3. 安全扫描器候选

## 3.1 Cisco AI Skill Scanner

可作为第一优先扫描器适配对象。

重点能力：

- Pattern / YAML / YARA；
- 行为数据流；
- LLM 语义审查；
- Prompt Injection / 数据泄露 / 恶意代码；
- SARIF；
- CI/CD；
- REST API / SDK；
- 可扩展规则。

注意：扫描器自己也明确说明“未发现风险不等于 Skill 安全”。

因此应把它作为自动门禁，而不是安全认证。

## 3.2 NVIDIA SkillSpector

可作为第二扫描器或交叉验证扫描器。

可见能力覆盖：

- Prompt Injection；
- Data Exfiltration；
- Privilege Escalation；
- Supply Chain；
- Tool Misuse；
- Dangerous Code；
- Taint Tracking；
- MCP Least Privilege / Tool Poisoning；
- 静态 + 可选 LLM 语义分析；
- JSON/Markdown/SARIF。

建议把多扫描器结果统一到公司的 Finding Schema，而不是在数据模型中写死某个工具字段。

---

## 4. 推荐 POC 打分维度

| 维度 | 权重 | 关键验证点 |
| --- | ---: | --- |
| 安全治理与发布门禁 | 25% | 扫描、审核、风险等级、强制门禁、撤销 |
| SSO/RBAC/职责分离 | 15% | 公司 IAM、管理员/审核员/作者分权 |
| Gerrit/SCM 接入 | 15% | webhook/event/API、版本追溯、增量导入 |
| 版本与不可变发布 | 10% | digest、历史、diff、回滚、不可篡改 |
| Scanner 扩展 | 10% | Cisco/NVIDIA/内部规则适配 |
| 私有部署与运维 | 10% | HA、DB、备份、K8s、日志、监控 |
| CLI/API/Agent 兼容 | 5% | 内部安装和自动化集成 |
| 二开成本 | 5% | 技术栈、代码结构、插件能力 |
| 社区成熟度 | 5% | release、issue、commit、文档、许可证 |

---

## 5. 统一 POC 测试集建议

准备 20~50 个样例 Skill：

1. 纯 Markdown 安全 Skill；
2. 带 references/assets；
3. 带 Python 脚本；
4. 带 Shell；
5. 带网络访问；
6. 带 MCP 调用；
7. 读取环境变量；
8. 恶意 Prompt Injection；
9. `curl | sh`；
10. 远程下载脚本执行；
11. 包含敏感数据样例；
12. 依赖存在 CVE；
13. Skill rename；
14. 删除 SKILL.md；
15. 只修改 scripts、不改 SKILL.md；
16. 多 Skill 同一 commit；
17. 外部 Skill 更新；
18. 高风险 Skill 风险接受；
19. 已发布 Skill 撤销；
20. 扫描器异常/超时。

所有候选平台使用相同测试集，避免凭 Demo 印象做决策。

---

## 6. 选型建议

### 场景 A：尽快落地公司私有 SkillHub

优先：**iflytek/skillhub**。

理由：现成功能与目标最匹配，可以把更多精力投入到 Gerrit 接入、公司 SSO 和安全策略，而不是重新开发 Registry 基础能力。

### 场景 B：公司要统一 AI 资产治理平台

优先：**Nacos 3.2+**。

特别是公司已经使用 Nacos，或者未来准备统一 MCP、Prompt、Agent、Skill。

### 场景 C：两者都无法满足强安全门禁

不要立即完全重写平台。

可以采用：

```text
开源 Registry
  + Gerrit Discovery Service
  + Security Scanner Service
  + Company Policy Engine
  + Review Workflow
  + Runtime Trust Client
```

只有当 Registry 核心数据模型、权限模型或发布流程不可扩展时，再考虑自研完整 SkillHub。
