# SkillHub 安全管理分阶段上线计划

## 1. 上线原则

不要在第一版就把所有公司 Gerrit 提交都绑定到尚未稳定的安全扫描服务。

推荐采用“**先看见 → 再治理 → 再阻断 → 最后强制可信源**”的渐进策略。

---

## Phase 0 — 规范确认

### 目标

先统一什么是 Skill、什么需要审查、谁负责审批。

### 工作

- Skill Package 规范；
- 风险分级；
- 安全 Checklist；
- 数据模型；
- 纳管仓库/branch；
- 角色/RACI；
- 例外流程。

### 门槛

未完成这些内容前，不建议开发复杂平台流程。

---

## Phase 1 — 可见性：全量发现但不阻断

### 目标

回答：

- 公司到底有多少 Skill？
- 在哪些仓库？
- 谁维护？
- 哪些带脚本/网络/高权限？

### 工作

- Gerrit baseline；
- Gerrit 增量事件；
- inventory；
- digest；
- 基础自动扫描；
- Dashboard。

### 策略

```text
发现风险 -> 告警/待办
不阻断 Gerrit 合入
```

### 退出条件

- 纳管仓库覆盖率达到目标；
- 误识别率可接受；
- 事件丢失有 reconciliation；
- Scanner 稳定运行。

---

## Phase 2 — SkillHub 上架强门禁

### 目标

所有通过公司官方 SkillHub 分发的 Skill 都必须已审查。

### 工作

- SkillHub 基础平台上线；
- 自动 Scanner；
- 人工 Review Queue；
- policy engine；
- Approved digest；
- publish gate；
- revoke/offline。

### 策略

```text
Gerrit 可继续合入
但未 Approved 的 Skill 不能上架到生产 SkillHub
```

### 优势

风险较小，且可以很快形成“可信发布中心”。

---

## Phase 3 — 高风险 Gerrit 合入门禁

### 目标

对 L2/L3 Skill，在进入正式 branch 前完成安全审查。

### 工作

- Gerrit Check/Label；
- Submit Requirement；
- Review 状态回写；
- Scanner SLA；
- 故障应急通道。

### 策略

```text
L0/L1：可保持异步/快速通道
L2/L3：未通过安全审查不得合入指定 branch
```

### 注意

必须先保证：

- Scanner 高可用；
- Review SLA；
- 平台故障处理；
- 紧急例外审批。

否则容易影响研发效率。

---

## Phase 4 — Runtime 可信源强制

### 目标

阻止“SkillHub 审批完整，但用户仍直接从公网/本地加载未审 Skill”的旁路。

### 工作

- 公司统一 CLI；
- Agent 默认 Registry；
- 安装时 digest 校验；
- revoke 查询；
- 本地 Skill 目录盘点；
- 终端管控；
- 关键环境公网 Registry 禁止策略。

### 策略

正式环境：

```text
Unknown source -> Block
Unapproved digest -> Block
Revoked digest -> Block
Approved internal SkillHub digest -> Allow
```

---

## Phase 5 — 高成熟安全能力

增强项：

- Published Skill 数字签名；
- Sigstore/企业签名体系；
- 动态沙箱执行评估；
- SBOM；
- 供应链 Provenance；
- 自动定期复审；
- Scanner 多引擎一致性；
- 规则平台；
- 威胁情报联动；
- Skill 使用影响分析。

---

## 2. 试点建议

第一批不要全公司上线。

选择一个：

- 有 20~100 个 Skill；
- 包含纯 Prompt 和脚本型 Skill；
- 有 Gerrit 使用经验；
- 有 CM/安全人员可配合；
- 对研发效率影响可观察。

的团队进行试点。

---

## 3. 历史 Skill 迁移策略

### 批次 A — L2/L3

优先处理高风险：

- 部署；
- CI/CD；
- 数据库；
- Shell；
- 网络；
- 凭据。

### 批次 B — L1

项目分析、只读工具等。

### 批次 C — L0

纯文档 Prompt/模板，可批量快速扫描与审批。

---

## 4. 上线检查表

### 平台

- [ ] 备份恢复验证
- [ ] SSO/RBAC 验证
- [ ] 管理员不可绕过审批
- [ ] 审计日志可查询
- [ ] Scanner 超时/失败可观察
- [ ] Queue/DB 监控
- [ ] 撤销功能验证

### Gerrit

- [ ] Baseline 完成
- [ ] Add/Modify/Delete/Rename/Copy 测试
- [ ] scripts-only change 测试
- [ ] 多 Skill commit 测试
- [ ] 重复事件幂等
- [ ] 漏事件 reconciliation

### 安全

- [ ] Prompt Injection 样例可检测
- [ ] Secret Exfiltration 样例可检测
- [ ] `curl | sh` 样例可阻断
- [ ] symlink/path traversal 防护
- [ ] 外部 Skill 固定 revision
- [ ] 审批 digest 防漂移

### Runtime

- [ ] 官方 CLI 只使用内网 Registry
- [ ] Unapproved Skill 安装阻断
- [ ] Revoked Skill 安装阻断
- [ ] digest 校验

---

## 5. 关键上线指标

建议阶段目标：

- Gerrit Skill 发现覆盖率 ≥ 95%，稳定后提升至接近 100%；
- SkillHub 发布 Skill 审查覆盖率 = 100%；
- L2/L3 人工审核覆盖率 = 100%；
- Critical 未接受风险 Finding 上架数 = 0；
- 已撤销 Skill 通过官方渠道再次安装成功数 = 0；
- Gerrit 事件补偿后 inventory 差异持续为 0；
- Review 超期率可监控并持续下降。

---

## 6. 建议的决策点

### Gate 1

POC 后决定：

- iflytek/skillhub；
- Nacos Skill Registry；
- 开源 + 自研扩展；
- 极端情况下完整自研。

### Gate 2

Phase 1 运行稳定后决定是否启用 SkillHub Publish Block。

### Gate 3

Publish Block 运行稳定、审核 SLA 达标后，决定是否对 L2/L3 启用 Gerrit Submit Block。

### Gate 4

客户端接入成熟后，再逐步启用 Runtime 强制可信源。
