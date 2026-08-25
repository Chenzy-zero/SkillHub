# SkillHub 安全管理需求拆分

## 1. 需求目标

建设公司级 Skill 安全治理能力，覆盖“发现、注册、扫描、审核、发布、分发、运行、变更、撤销、审计”全生命周期。

---

## R1. Skill 统一资产模型

### R1.1 Skill 标识

系统必须为每个 Skill 提供稳定 `skill_id`，不能只依赖名称。

### R1.2 来源绑定

必须记录：

- SCM 类型；
- repository；
- branch；
- path；
- Gerrit Change-Id / revision；
- owner/team。

### R1.3 内容身份

必须对完整 Skill Package 计算 `skill_digest`。

### 验收标准

- 同名不同仓库 Skill 可区分；
- 同一内容在不同 commit 可识别为相同 digest；
- 任一包内文件变化导致 digest 变化。

---

## R2. SkillHub 平台选型与 POC

### 范围

至少 POC：

- iflytek/skillhub；
- Nacos 3.2+ Skill Registry。

### 验证项

- 私有部署；
- SSO/RBAC；
- 注册/审核/上架；
- 版本；
- 审计；
- CLI/API；
- Scanner 插件；
- Gerrit 接入；
- HA/备份；
- 二开成本。

### 验收标准

输出量化打分表和推荐结论。

---

## R3. Gerrit Skill 自动发现

### R3.1 服务端事件

系统应消费 Gerrit 服务端事件，不依赖开发者本地 Hook。

### R3.2 变更文件识别

支持：A/M/D/R/C。

### R3.3 Skill Root Resolver

任何 Skill Root 内文件变化都能映射到对应 Skill。

### 验收标准

以下场景全部正确识别：

- 新增 SKILL.md；
- 只改 scripts；
- 删除 Skill；
- rename Skill；
- 一个 commit 改多个 Skill。

---

## R4. Gerrit 历史基线盘点

### 需求

在增量监听上线前，对纳管仓库执行全量 `SKILL.md` 发现。

### 验收标准

- 可输出仓库 Skill 清单；
- 可重新运行且结果幂等；
- 与后续增量 inventory 对齐。

---

## R5. 自动安全扫描

### R5.1 Scanner Adapter

必须支持多个扫描器。

首批候选：

- Cisco AI Skill Scanner；
- NVIDIA SkillSpector。

### R5.2 标准化 Finding

扫描结果必须转换为统一 schema。

### R5.3 内网模式

核心静态扫描应支持离线/纯内网运行。

### 验收标准

- 同一 Skill 可同时运行多个扫描器；
- 扫描结果可统一展示；
- Critical/High 可触发阻断策略。

---

## R6. 人工安全审查工作流

### 功能

- 待审队列；
- 分配 Reviewer；
- Skill diff；
- Scanner Findings；
- Approve；
- Reject；
- Risk Acceptance；
- 审核意见；
- 审核历史。

### 验收标准

审批必须绑定当前 digest；审核过程中 Skill 发生变化时旧审批无法提交。

---

## R7. 版本与 Digest 管理

### 需求

- 不可变版本；
- 当前版本；
- 历史版本；
- 上一个 Approved digest；
- diff；
- rollback 元数据。

### 验收标准

已发布版本无法原地覆盖。

---

## R8. 上架、下架、撤销

### 状态

至少支持：

- DRAFT/DISCOVERED；
- REVIEWING；
- APPROVED；
- PUBLISHED；
- OFFLINE；
- REVOKED；
- STALE。

### 验收标准

被 REVOKED 的 digest 不能继续通过官方 CLI 安装。

---

## R9. 内网分发与客户端来源限制

### 需求

- 公司统一 Registry 地址；
- CLI/Agent 只从内部 Registry 获取 Skill；
- 安装时验证 digest；
- 可查询 Skill 审批状态；
- 可获取撤销列表。

### 后续增强

- 数字签名；
- 终端侧旁路检测；
- 强制可信源策略。

---

## R10. 外部 Skill 隔离导入

### 流程

```text
URL/Git/ZIP
 -> Quarantine
 -> 固定 source revision
 -> License 检查
 -> 文件校验
 -> 扫描
 -> 审核
 -> 内网发布
```

### 验收标准

禁止运行时直接追踪外部 `latest/main`。

---

## R11. RBAC / SSO / 职责分离

### 角色

至少：

- Author；
- Owner；
- Reviewer；
- Skill Admin；
- Auditor；
- Platform Admin。

### 关键要求

管理员不能因为是平台管理员而默认绕过安全审批。

---

## R12. 审计与报表

### 审计对象

- Skill 导入；
- 内容变更；
- 扫描；
- 审核；
- 发布；
- 撤销；
- 风险接受；
- 管理员操作。

### 报表

- Skill 总数；
- 未审查数量；
- 风险等级分布；
- 超期审核；
- 高危 Findings；
- 各团队 Skill 数；
- 外部来源 Skill 数。

---

## R13. 可靠性与异常处理

### 要求

- 事件幂等；
- 队列；
- 重试；
- 死信；
- 对账；
- Scanner 超时处理；
- 平台不可用降级；
- 告警。

### 原则

异步发现系统故障不应直接拖垮 Gerrit；正式发布门禁可以 Fail Closed。

---

## R14. 安全策略与例外管理

### 需求

- policy version；
- 风险等级规则；
- 自动放行条件；
- 强制人工审查条件；
- finding severity override；
- exception；
- exception expiry；
- revoke。

### 验收标准

能够回答：“这个 Skill 为什么在这个时间点被批准？”

---

## 2. 非功能需求

### 性能

- Gerrit Event Collector 轻量返回；
- 普通增量检测秒级入队；
- 深度扫描异步执行。

### 安全

- 不执行不可信 Skill 脚本；
- 上传/解压防路径穿越；
- 密钥不写日志；
- 服务使用最小权限账户。

### 可维护性

- Scanner 插件化；
- Policy 与平台解耦；
- SCM Connector 可扩展；
- 数据库 schema 版本化。

### 可观测性

至少监控：

- event lag；
- queue depth；
- scan duration；
- scan failure；
- review backlog；
- publish block；
- reconciliation mismatch。

---

## 3. MVP 范围

### 必须

- R1、R3、R4、R5、R6、R7、R8；
- R2 完成 POC 决策；
- R9 至少实现官方 CLI 的内网可信源；
- R11 基础 RBAC；
- R12 基础审计。

### 可第二阶段

- 数字签名；
- 动态沙箱；
- 端点强制；
- 智能自动修复；
- 复杂推荐系统；
- 多地域部署。
