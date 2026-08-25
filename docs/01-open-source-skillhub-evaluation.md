# 开源 SkillHub / Skill Registry 方案调研

> 调研日期：2026-08-25  
> 当前状态：平台初步选型已完成，当前由项目侧自行搭建 `iflytek/skillhub` 做测试和公司内网适配。  
> 本文后续作为选型背景和备选方案记录保留。

## 1. 当前决策

当前实施路径：

```text
Gerrit 内部 Skill 治理
        +
公司自有发现/版本/扫描/审核数据模型
        +
iflytek SkillHub 作为 Registry / 发布管理平台
```

第一阶段不继续进行 Nacos 与 iflytek 的双轨 POC，先集中验证 iflytek SkillHub：

- 私有化部署；
- Skill 生命周期；
- Draft/Publish；
- API/CLI；
- RBAC；
- 审计；
- 自带 Scanner；
- 与公司 Gerrit 治理数据库的映射；
- 二次开发和公司 SSO 适配成本。

Nacos、saker-ai/skillhub 等仍保留为后续备选。如果 iflytek 在关键安全、权限、审计或扩展能力上无法满足公司要求，再重新进入平台选型。

---

## 2. iflytek/skillhub

### 定位

企业级、自托管 Agent Skill Registry，强调发布、发现、版本、命名空间、RBAC、审计、安全扫描和私有化部署。

### 当前重点验证能力

- Web UI + REST API + CLI；
- Skill 创建、版本、标签；
- Draft / Review / Publish / Offline 生命周期；
- 命名空间；
- 平台/命名空间 RBAC；
- 审计日志；
- 安全 Scanner 服务；
- Docker / Kubernetes 部署；
- PostgreSQL、Redis、对象存储等部署依赖；
- 公司 SSO/OAuth/OIDC 适配；
- Gerrit 自动发现后的同步接口。

### 与本项目的职责边界

不建议把公司所有安全治理逻辑完全依赖在 SkillHub 内部。

公司侧仍保留：

- Canonical Skill；
- Skill Source；
- Source Revision；
- SHA-256 Content Version；
- Scanner/Policy 版本；
- CM Review；
- Gerrit 审计链。

SkillHub 主要承担：

- Registry；
- Skill 展示/检索；
- 平台版本；
- 正式发布；
- 分发；
- 平台 RBAC/审计；
- 可选的额外安全扫描。

### POC/验证问题

1. 未审核 Skill 能否创建为 Draft；
2. Publish 是否可以通过 API 被公司 Gate 控制；
3. 管理员是否存在绕过扫描/审核的发布路径；
4. SkillHub 版本如何映射到公司 Content Version；
5. 能否保存/查询源仓库、commit、digest 等扩展元数据；
6. 自带 Scanner 的版本和规则是否可追溯；
7. Scanner 结果是否可通过 API 获取；
8. Skill 删除/下架/撤销语义；
9. API 幂等和失败重试方式；
10. 数据备份、恢复、升级兼容性。

---

## 3. Nacos 3.2+ Skill Registry（备选）

### 价值

Nacos Skill Registry 的主要优势在于未来可能统一治理：

- Skill；
- Prompt；
- MCP；
- Agent。

如果公司未来要建设统一 AI Registry 控制面，Nacos 仍值得重新评估。

### 当前不作为第一阶段主线的原因

- 当前已经选择优先搭建 iflytek SkillHub；
- 第一阶段更需要尽快验证 Gerrit -> 扫描 -> CM Review -> SkillHub 的治理闭环；
- 同时维护两个 POC 会分散实现精力。

如果 iflytek 在关键能力上失败，再启动 Nacos 对比测试。

---

## 4. 其他参考项目

### saker-ai/skillhub

价值：

- 轻量自托管；
- Git-native 思路；
- 可参考 Webhook/仓库版本设计。

不足：企业审核、安全扫描、SSO/审计能力需要进一步确认。

### airopshq/skillshub

价值：

- Git 作为 Skill 单一事实源；
- CLI/MCP 同步；
- 多 Agent 分发方式。

更适合作为同步/客户端设计参考，不作为当前安全治理平台。

### 公共 Skill 目录类项目

可参考：

- 搜索；
- 分类；
- 元数据；
- 安装体验。

不作为公司内网安全治理底座。

---

## 5. 安全扫描器方向

公司治理系统应支持 Scanner Adapter，而不是把数据模型写死到 SkillHub 自带 Scanner。

首版可以优先使用：

- iflytek SkillHub 自带 Scanner；或
- 公司当前能快速部署的一个自动扫描工具。

后续可评估：

- Cisco AI Skill Scanner；
- NVIDIA SkillSpector；
- 公司自定义规则引擎。

统一扫描记录至少包括：

```text
content_version
scanner_name
scanner_version
policy_version
status
findings
risk_level
raw_report_ref
```

---

## 6. 当前 iflytek SkillHub 验证清单

### 平台

- [ ] 单机测试环境部署成功
- [ ] 数据库初始化/升级方式确认
- [ ] 备份恢复方式确认
- [ ] 用户/RBAC 验证
- [ ] 审计日志验证

### Skill 生命周期

- [ ] Create/Register
- [ ] Draft
- [ ] Review
- [ ] Publish
- [ ] New Version
- [ ] Offline
- [ ] Revoke/Delete 语义

### API

- [ ] 创建 Skill
- [ ] 上传版本
- [ ] 查询状态
- [ ] Publish
- [ ] Offline
- [ ] 获取 Scanner 结果
- [ ] 查询审计

### 公司数据模型映射

- [ ] Canonical Skill -> SkillHub Skill
- [ ] Skill Source 元数据如何保留
- [ ] Source Revision 如何追溯
- [ ] Content Version/Digest 如何映射平台版本
- [ ] 一个 Content Version 被多个 Source 引用时如何处理
- [ ] Draft 与公司 Review Status 如何映射

### Scanner

- [ ] 扫描触发方式
- [ ] 扫描范围
- [ ] Scanner Version
- [ ] Finding 格式
- [ ] Re-scan
- [ ] API 查询
- [ ] 扫描失败/超时

---

## 7. 重新启动平台选型的触发条件

如果出现以下情况，应重新评估 Nacos / 其他平台 / 自研：

1. iflytek 无法满足公司 SSO/RBAC；
2. Publish 流程无法可靠接入公司安全 Gate；
3. 数据模型无法保存或关联公司来源版本/digest；
4. API 不足以实现自动同步；
5. 审计能力不能满足追溯要求；
6. 高可用/备份/升级风险不可接受；
7. 二开成本超过替代方案；
8. 公司战略转为统一 Skill + MCP + Prompt + Agent Registry。

当前不建议在这些问题尚未验证前启动完整自研。
