# SkillHub 安全管理分阶段上线计划

> 版本：v0.2

## 1. 上线原则

当前阶段不追求一次性覆盖所有 Skill 来源和 Runtime 场景。

优先顺序：

```text
先把 Gerrit 中已有 Skill 看清楚
 -> 再把版本和内容识别做准确
 -> 再接自动扫描与 CM 审核
 -> 再进入 SkillHub 正式纳管
 -> 最后再考虑更强的运行时和外部来源治理
```

核心目标是先建立一个稳定、可追溯、不会漏 Skill 变更的内部治理闭环。

---

## Phase 0 — 规范与模型确认

### 目标

统一 Skill 在系统中的定义。

### 必须确认

- `SKILL.md` 是识别锚点；
- Skill Root = `SKILL.md` 所在目录；
- Skill Package 文件边界；
- Skill Source key；
- Canonical Skill；
- Source Revision；
- SHA-256 Content Version；
- Scan/Review/SkillHub 状态分离。

### 退出条件

以下设计没有重大歧义：

```text
Source 如何识别
Revision 如何产生
Digest 如何计算
相同 Digest 如何复用
Source 如何关联到 Canonical Skill
```

---

## Phase 1 — iflytek SkillHub 测试环境

### 目标

先搞清楚平台能提供什么，不急着把所有治理逻辑塞进 SkillHub。

### 工作

- 搭建 iflytek SkillHub；
- 验证用户/RBAC；
- 验证 Skill 创建、Draft、Publish、Offline；
- 验证版本模型；
- 验证 API/CLI；
- 验证自带 Scanner；
- 验证审计；
- 确定公司系统与 SkillHub 的映射方式。

### 关键决策

确认采用：

```text
模式 A：Approved 后才 Register/Publish
```

还是：

```text
模式 B：发现后创建 Draft，Approved 后 Publish
```

### 退出条件

- SkillHub 基础功能可用；
- API 清单明确；
- Draft/Publish 状态语义明确；
- Scanner 输出可读取；
- 同步失败有处理方案。

---

## Phase 2 — Gerrit 可见性：Baseline + 增量发现

### 目标

回答：

- 公司 Gerrit 到底有多少 Skill Source；
- 分布在哪些仓库和路径；
- 每个 Source 当前是什么 commit；
- 哪些 Skill Root 正在发生变化。

### 工作

- Baseline 全量扫描；
- Gerrit 服务端 Hook/Event；
- Changed Files Resolver；
- Skill Root Resolver；
- Skill Source；
- Source Revision；
- Rename/Delete/Move；
- Reconciliation。

### 策略

```text
发现问题 -> 记录/告警
不阻塞 Gerrit 合入
```

### 退出条件

- Baseline 可重复执行；
- A/M/D/R/C 用例全部通过；
- scripts-only change 可识别；
- 多 Skill commit 可识别；
- 事件重复幂等；
- 对账差异可以修复。

---

## Phase 3 — Content Version 与自动安全扫描

### 目标

把“commit 版本”升级为“来源版本 + 内容版本”。

### 工作

- Skill Package Snapshot；
- Manifest；
- SHA-256 Digest；
- Content Version 去重；
- Scan Queue；
- Scanner Adapter；
- iflytek 自带 Scanner 或首个公司 Scanner；
- Finding 标准化；
- 定时补扫。

### 策略

```text
新 Revision -> 计算 Digest
新 Digest -> 扫描
已有 Digest -> 检查是否可复用扫描结论
```

### 退出条件

- Digest 在不同节点计算一致；
- 内容变化必然产生新 digest；
- 相同内容不会重复创建 Content Version；
- Scanner 超时/失败可重试；
- 实时与定时扫描不会重复失控。

---

## Phase 4 — CM Review 与 SkillHub 纳管

### 目标

形成真正的安全闭环。

### 工作

- CM Review Queue；
- Scan Findings 展示；
- Revision diff；
- Content Version 审核；
- Approve/Reject；
- Canonical Skill 关联；
- SkillHub Sync Worker；
- Draft/Publish；
- 审计。

### 推荐流程

```text
Gerrit发现
 -> Source Revision
 -> Content Version
 -> Scanner
 -> CM Review
 -> APPROVED
 -> SkillHub
```

### 关键规则

- Review 状态与 SkillHub 状态分离；
- 相同 digest 可按有效策略复用安全结论；
- SkillHub API 失败不覆盖安全审核事实；
- 未 APPROVED 的 Content Version 不得误标正式 Published。

### 退出条件

- 一个 SkillHub Published 版本可以完整追溯到 Gerrit Source/Revision/Digest；
- CM 可查看 Scanner Evidence；
- SkillHub 同步支持重试；
- Review Backlog 可监控。

---

## Phase 5 — 灰度运行和制度化

### 目标

先在代表性仓库稳定运行，再扩大覆盖。

### 试点建议

优先选择同时包含：

- 纯 Markdown Skill；
- 脚本型 Skill；
- 多 Skill 仓库；
- 多次修改；
- rename/move；
- 相同 Skill 多引用来源。

### 灰度步骤

#### 阶段 1：只记录

```text
发现 -> 扫描 -> Review 待办
```

不影响开发流程。

#### 阶段 2：SkillHub 发布门禁

```text
未 APPROVED -> 不允许正式 SkillHub Publish
```

#### 阶段 3：根据实践决定是否增加 Gerrit Gate

只在 Scanner SLA、审核 SLA、故障处理成熟后评估。

不建议第一阶段直接对所有 Gerrit 提交 Fail Closed。

---

## 2. 历史 Skill 迁移策略

### 第一步：只建立 Source/Revision/Digest

Baseline 阶段先把资产看全，不要求同步阻塞完成全部审核。

### 第二步：批量自动扫描

按队列处理历史 Content Version。

相同 digest 只需扫描一次。

### 第三步：CM 分批审核

建议优先级：

1. 带脚本；
2. 带网络/MCP/凭据操作；
3. DevOps/CI/CD/发布类；
4. 普通只读 Skill；
5. 纯 Markdown Skill。

### 第四步：SkillHub 纳管

通过审核后进入 SkillHub 正式管理。

---

## 3. 上线检查表

### Skill 识别

- [ ] `SKILL.md` Root 规则确认
- [ ] Skill Package 范围确认
- [ ] repository + path + name Source key 确认
- [ ] branch 语义确认
- [ ] nested SKILL.md 规则确认

### Gerrit

- [ ] Baseline 完成
- [ ] Add 测试
- [ ] Modify 测试
- [ ] scripts-only 测试
- [ ] Delete 测试
- [ ] Rename/Move 测试
- [ ] Copy 测试
- [ ] 一个 commit 多 Skill 测试
- [ ] 重复事件幂等
- [ ] Reconciliation

### Digest

- [ ] SHA-256
- [ ] 路径排序稳定
- [ ] 文件 mode 规则确认
- [ ] LFS 规则确认
- [ ] symlink 规则确认
- [ ] 不同节点一致性测试
- [ ] 相同内容去重测试

### Scanner

- [ ] 新 digest 自动扫描
- [ ] 已有 digest 复用逻辑
- [ ] Scanner version 入库
- [ ] Policy version 入库
- [ ] Timeout/Retry
- [ ] 定时补扫
- [ ] Findings 可追溯

### CM Review

- [ ] 待审队列
- [ ] Revision 信息
- [ ] Digest 信息
- [ ] Diff
- [ ] Findings
- [ ] Approve/Reject
- [ ] 历史记录
- [ ] Canonical Link/Unlink

### SkillHub

- [ ] 测试环境可用
- [ ] Draft/Publish 语义确认
- [ ] API 权限确认
- [ ] Scanner 能力确认
- [ ] Sync Retry
- [ ] Review 与 Sync 状态分离
- [ ] Published 可追到 Digest/Revision

---

## 4. 第一阶段指标

建议监控：

- Gerrit 纳管仓库覆盖率；
- Skill Source 数量；
- Canonical Skill 数量；
- Source Revision 数量；
- Content Version 数量；
- Digest 去重复用率；
- 未扫描 Content Version 数；
- Scan Failed 数；
- CM Review Backlog；
- Approved 数；
- SkillHub Published 数；
- SkillHub Sync Failed 数；
- Reconciliation mismatch 数。

---

## 5. 后续阶段路线图

等 Gerrit 内部治理稳定后，再评估：

### Phase 6 — 更强风险策略

- L0/L1/L2/L3；
- 自动放行；
- Security Escalation；
- Policy Engine；
- 多 Scanner 交叉验证。

### Phase 7 — 外部 Skill 引入

- GitHub/公网 Skill；
- Import Quarantine；
- 来源固定；
- License / 供应链治理。

### Phase 8 — Runtime 可信源

- 内网 SkillHub 默认源；
- Digest/签名验证；
- Runtime 拦截；
- 本地旁路检测。

这些不应阻塞当前 Gerrit 内部 Skill 治理 MVP。
