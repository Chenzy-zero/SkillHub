# SkillHub 安全管理需求拆分

> 版本：v0.2  
> 第一阶段范围：Gerrit 内部 Skill 发现、版本、安全扫描、CM 审核与 iflytek SkillHub 纳管

## 1. 总体目标

建设公司 Gerrit 内 Skill 安全治理闭环：

```text
发现 -> Source 登记 -> Revision 追溯 -> Digest 内容版本 -> 自动扫描 -> CM审核 -> SkillHub纳管 -> 审计
```

---

## R1. Skill 统一资产模型

### R1.1 Canonical Skill

系统必须支持逻辑 Skill 身份，用于关联多个引用来源。

### R1.2 Skill Source

首版以：

```text
repository + skill_path + skill_name
```

识别一个 Skill Source。

任一字段不一致时先独立登记，不在自动发现阶段强制合并。

### R1.3 Source Revision

同一 Source 的不同 commit/revision 必须保留为独立来源版本。

### R1.4 Content Version

完整 Skill Package 必须计算 SHA-256 `skill_digest`，形成内容版本。

### R1.5 多 Source 关联

多个 Skill Source 可后续关联到一个 Canonical Skill，且：

- 不删除原 Source；
- 不改写 Revision 历史；
- 关联/拆分操作可审计。

### 验收标准

- 同名不同仓库/路径可以独立登记；
- 同一 Source 不同 commit 均可追溯；
- 不同 commit 相同内容可识别为相同 digest；
- 不同 Source 相同内容可识别为相同 Content Version；
- Canonical 合并不丢失来源历史。

---

## R2. iflytek SkillHub 搭建与适配

### R2.1 平台部署

完成 iflytek SkillHub 测试环境部署。

### R2.2 能力验证

至少验证：

- Skill 创建/注册；
- Draft / 审核 / Publish 能力；
- 版本管理；
- API/CLI；
- RBAC；
- 审计；
- 自带安全扫描；
- 上架/下架；
- 数据库与备份方式。

### R2.3 公司治理对接

明确：

- 何时创建 SkillHub Draft；
- 何时允许 Publish；
- 如何映射 Canonical Skill / Source / Content Version；
- 如何同步审核状态；
- SkillHub 自带扫描结果如何入库或引用；
- SkillHub 不可用时如何重试。

### 验收标准

输出 iflytek SkillHub 集成方案和接口清单。

---

## R3. Gerrit 服务端 Skill 自动发现

### R3.1 统一服务端触发

系统必须由 Gerrit 服务端 Hook/Event/Plugin 触发检查，不依赖开发者客户端 Hook。

### R3.2 Changed Files

支持：

- Add；
- Modify；
- Delete；
- Rename；
- Copy。

### R3.3 Skill Root Resolver

以 `SKILL.md` 所在目录为 Skill Root。

Skill Root 内任意受管控文件变化均应映射到该 Skill。

### 验收标准

以下场景全部正确识别：

- 新增 `SKILL.md`；
- 只修改 scripts、不修改 `SKILL.md`；
- 修改 references/config；
- 删除 `SKILL.md`；
- Skill 目录 rename/move；
- 一个 commit 修改多个 Skill；
- Copy 一个已有 Skill。

---

## R4. Gerrit Baseline 全量盘点

### 需求

系统上线前对纳管仓库和 branch 执行全量 `SKILL.md` 搜索。

每个发现项生成：

- Skill Source；
- 当前 Source Revision；
- Content Version；
- 待扫描任务。

### 验收标准

- 可输出完整 Skill Source 清单；
- 重复执行结果幂等；
- Baseline 与增量流程使用同一数据模型；
- 支持后续 reconciliation 对账。

---

## R5. SHA-256 Digest / Content Version

### R5.1 Digest 计算

对完整 Skill Package 计算 SHA-256。

### R5.2 规范化规则

必须定义：

- 文件排序；
- 相对路径；
- 文件内容；
- file mode；
- 换行符；
- symlink；
- LFS；
- submodule；
- 忽略文件规则。

### R5.3 去重

相同 digest 关联已有 Content Version，不重复创建内容版本。

### 验收标准

- 同一 Git tree 在不同执行节点得到一致 digest；
- 任一纳管文件真实内容变化导致 digest 变化；
- 不使用 MD5 作为安全完整性标识。

---

## R6. 自动安全扫描

### R6.1 扫描触发

支持：

- Gerrit 新 Content Version 实时触发；
- 定时批量扫描；
- SkillHub 内置 Scanner 结果接入；
- 后续第三方/自研 Scanner Adapter。

### R6.2 扫描结果

至少记录：

- content_version；
- scanner_name；
- scanner_version；
- policy_version；
- status；
- findings；
- risk level / score；
- raw report。

### R6.3 幂等

同一：

```text
content_version + scanner + scanner_version + policy_version + scan_mode
```

不得重复创建等价扫描任务。

### R6.4 扫描范围

至少覆盖：

- Prompt/Instruction 风险；
- 脚本危险行为；
- 文件/网络访问；
- 凭据访问；
- MCP/Tool；
- 外部 URL；
- 依赖与安装脚本；
- 敏感信息；
- 动态下载/执行；
- 混淆代码。

---

## R7. CM 审核工作流

### 功能

- 待审核列表；
- Source/Revision/Digest 信息；
- Scanner Findings；
- 与上一个 Revision diff；
- 与上一个 Approved Content Version diff；
- Approve；
- Reject；
- Request Changes；
- Exception/Escalate 预留；
- 审核历史。

### 规则

CM 审核结论以 Content Version 为主要对象。

高风险、扫描无法判断或例外问题可以升级安全人员/专家。

### 验收标准

能够回答：当前 Source 最新 Revision 对应的 digest 是否已经通过有效审核。

---

## R8. 安全结论复用

### 需求

当新 Revision 的 digest 已存在时，系统应检查是否可以复用已有扫描/审核结论。

复用条件至少考虑：

- digest 相同；
- scanner/version 有效；
- policy_version 有效；
- 原结论未失效/撤销；
- 无强制复扫规则。

### 验收标准

相同内容不会因为 cherry-pick 或多个引用来源产生无意义重复人工审核。

---

## R9. SkillHub 同步与发布

### R9.1 状态分离

安全 Review 状态和 SkillHub Sync 状态必须独立。

### R9.2 Publish Gate

未满足公司安全审核策略的 Content Version 不得被标记为正式 Published。

### R9.3 Draft

若 iflytek SkillHub 支持 Draft，可提前同步资产，但 Draft 不等同于安全通过。

### 验收标准

- APPROVED 后可自动/人工触发 SkillHub 同步；
- SkillHub API 失败可重试；
- 同步失败不覆盖审核历史；
- SkillHub 发布记录可追溯到 Source Revision 和 digest。

---

## R10. Rename / Move / Delete 生命周期

### Rename / Move

路径变化导致 Source key 变化时：

- 新路径创建新 Source；
- 旧 Source 标记 MOVED/INACTIVE；
- 保存迁移关系；
- 可后续关联同一 Canonical Skill。

### Delete

删除后：

- Source 标记 DELETED/INACTIVE；
- 历史 Revision/Scan/Review 不删除；
- 已发布 SkillHub 版本是否下架由治理策略决定。

---

## R11. 状态模型

至少分离：

### Scan Status

```text
NOT_SCANNED / PENDING / RUNNING / PASSED / FAILED / ERROR / TIMEOUT
```

### Review Status

```text
NOT_REVIEWED / PENDING / APPROVED / REJECTED / EXCEPTION / STALE
```

### SkillHub Status

```text
NOT_SYNCED / DRAFT / PUBLISHED / OFFLINE / REVOKED / ERROR
```

不得仅使用 Boolean “是否安全审查”。

---

## R12. 审计

必须记录：

- Source 创建/结束；
- Revision 创建；
- Content Version 创建/复用；
- Canonical 关联/拆分；
- Scan 任务；
- Review；
- SkillHub 同步/发布/下架；
- 管理员手工操作。

### 验收标准

能够完整追踪一个 SkillHub 版本的 Gerrit 来源、安全扫描和审核链路。

---

## R13. 幂等、重试和对账

### 要求

- Gerrit 事件去重；
- Source Revision 唯一约束；
- Content Version digest 去重；
- Scan 任务幂等；
- SkillHub 同步重试；
- 定时 Reconciliation；
- Scanner 超时/错误处理；
- 告警和死信。

### 原则

发现/扫描系统故障不应直接拖垮 Gerrit 主流程；第一阶段优先异步治理。

---

## 2. 非功能需求

### 性能

- Gerrit 服务端触发逻辑必须轻量；
- 大仓库不能每次提交全仓扫描；
- 使用 changed files -> Skill Root 的增量算法；
- 深度扫描异步执行。

### 安全

- 扫描阶段不直接执行不可信 Skill 脚本；
- 服务账号最小权限；
- 密钥不写日志；
- Skill Package 读取和解压流程防路径穿越。

### 可维护性

- Scanner Adapter 可扩展；
- SkillHub Adapter 与安全数据模型解耦；
- Policy Version 可追溯；
- Schema 可迁移。

### 可观测性

至少监控：

- Gerrit event lag；
- discovery failure；
- scan queue depth；
- scan duration/failure；
- review backlog；
- SkillHub sync failure；
- reconciliation mismatch。

---

## 3. 第一阶段 MVP

### P0

- R1：资产模型；
- R3：Gerrit 服务端发现；
- R4：Baseline；
- R5：Digest；
- R6：自动扫描基础能力；
- R7：CM Review；
- R9：SkillHub 同步；
- R11/R12/R13：状态、审计、幂等基础能力。

### P1

- Canonical Skill 多 Source 手工关联；
- 安全结论智能复用；
- SkillHub Draft 自动同步；
- 第二扫描器；
- 更完整 Policy Engine。

### 后续阶段

暂不纳入首版：

- 外部 Skill 引入治理；
- Runtime 可信源强制；
- 终端旁路检测；
- 数字签名；
- 动态沙箱；
- 全公司 Gerrit Submit Block。
