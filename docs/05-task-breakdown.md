# SkillHub 安全管理项目任务拆分（WBS）

## 总体里程碑

| 里程碑 | 目标 | 建议周期 |
| --- | --- | --- |
| M0 | 规范、盘点、数据模型 | 1~2 周 |
| M1 | 开源平台双轨 POC | 2~3 周 |
| M2 | Gerrit 自动发现 MVP | 2~4 周 |
| M3 | 自动安全扫描与策略引擎 | 2~4 周 |
| M4 | 人工审核与发布门禁 | 2~4 周 |
| M5 | 内网分发与运行时可信源 | 2~4 周 |
| M6 | 试点、灰度、制度化 | 2~4 周 |

> 周期为粗略估算，应结合公司现有 Nacos/Gerrit/IAM/基础设施实际情况修订。

---

## M0 — 规范与盘点

### T0.1 定义 Skill 识别规范

- 明确 Skill Root；
- 明确是否允许嵌套 `SKILL.md`；
- 明确允许文件类型；
- 明确 LFS/submodule/symlink 规则；
- 明确正式纳管 branch。

**输出**：Skill Package 规范 v0.1。

### T0.2 定义 Skill 风险分级

- L0/L1/L2/L3；
- 自动放行规则；
- 人工审核规则；
- 双人审批规则。

**输出**：风险分级矩阵。

### T0.3 定义安全审核 Checklist

覆盖 Prompt、工具、脚本、网络、依赖、凭据、生产权限等。

**输出**：Review Checklist v0.1。

### T0.4 定义数据模型

- skill；
- skill_version；
- source_binding；
- scan_result；
- finding；
- review_record；
- audit_event；
- exception。

**输出**：ER/Schema 草案。

### T0.5 Gerrit 全量 baseline 方案

- 纳管仓库清单；
- branch；
- 扫描方式；
- 权限账户；
- 性能评估。

---

## M1 — SkillHub 开源平台 POC

### T1.1 部署 iflytek/skillhub

验证：

- 单机/K8s；
- PostgreSQL/Redis/对象存储；
- 用户和 RBAC；
- Skill 发布；
- 安全扫描；
- CLI/API；
- 审计。

### T1.2 部署 Nacos 3.2+ Skill Registry

验证：

- Skill 生命周期；
- Pipeline；
- Namespace；
- CLI/API/SDK；
- 权限；
- 审计；
- 扫描扩展。

### T1.3 准备统一测试集

至少 20 个 Skill，包含安全/危险/异常/边界用例。

### T1.4 执行 POC 评分

按：

- 安全治理；
- SSO/RBAC；
- Gerrit；
- Scanner；
- 运维；
- CLI/API；
- 稳定性；
- 二开成本。

### T1.5 形成 ADR

**输出**：`ADR-001 SkillHub 基础平台选型`。

---

## M2 — Gerrit 自动发现 MVP

### T2.1 Gerrit Event Collector

- patchset-created；
- ref-updated；
- 认证；
- event id；
- 日志。

### T2.2 Message Queue

- topic；
- retry；
- dead letter；
- lag monitor。

### T2.3 Gerrit Change Resolver

获取：

- project；
- branch；
- Change-Id；
- patchset；
- revision；
- changed files；
- old/new path。

### T2.4 Skill Root Resolver

支持 A/M/D/R/C。

### T2.5 Skill Package Snapshot

- 读取完整目录；
- 文件白名单；
- 大小限制；
- symlink 处理。

### T2.6 Digest Service

- 规范化；
- SHA-256；
- 测试稳定性。

### T2.7 Inventory Service

- 新建 Skill；
- 新版本；
- rename；
- delete；
- current projection。

### T2.8 Baseline Scanner

一次性全 Gerrit 查找 `SKILL.md` 并登记。

### T2.9 Reconciliation Job

定时对账，补偿漏事件。

---

## M3 — 自动安全扫描

### T3.1 Scanner Adapter 接口

统一：

```text
scan(skill_package, policy) -> normalized_scan_result
```

### T3.2 Cisco Skill Scanner Adapter

- CLI/API 集成；
- JSON/SARIF 解析；
- 超时；
- 重试。

### T3.3 NVIDIA SkillSpector Adapter

- 静态模式；
- 可选内网 LLM 模式；
- 输出归一化。

### T3.4 自定义公司规则

首批规则：

- 禁止公网 Registry；
- 禁止特定域名；
- 禁止 `curl | sh`；
- 凭据目录；
- 高危 Shell；
- 生产环境关键字；
- 公司内部敏感路径。

### T3.5 Policy Engine

输入：

- risk level；
- findings；
- package metadata；
- source；
- policy version。

输出：

- AUTO_APPROVE；
- REVIEW_REQUIRED；
- BLOCK；
- EXCEPTION_REQUIRED。

### T3.6 Scanner 可观测性

- duration；
- failure；
- timeout；
- queue；
- finding count。

---

## M4 — 人工审核与发布门禁

### T4.1 Review Queue

- 待审核列表；
- owner/team；
- risk；
- SLA；
- reviewer。

### T4.2 Review Detail

展示：

- SKILL.md；
- scripts；
- diff；
- findings；
- external URLs；
- dependencies；
- source metadata。

### T4.3 审核操作

- Approve；
- Reject；
- Accept Risk；
- Request Changes。

### T4.4 Digest 并发校验

审批提交时检查当前 digest 与被审核 digest 一致。

### T4.5 SkillHub Publish Gate

所有 Web/API/CLI/Admin 发布入口统一检查 Policy。

### T4.6 Revoke / Offline

- 立即撤销；
- 原因；
- 审计；
- 客户端阻断。

### T4.7 Gerrit Check/Label（可选阶段）

高风险 Skill 合入前要求安全 Check。

---

## M5 — 内网分发与运行时可信源

### T5.1 公司 CLI 配置

- 默认 Registry；
- Token；
- Namespace；
- 代理/证书。

### T5.2 Agent 集成

验证：

- Codex；
- Claude Code；
- Cursor；
- Gemini CLI；
- 公司自研 Agent。

### T5.3 安装时校验

- approved status；
- digest；
- revoked status。

### T5.4 外部 Skill 导入网关

- Git URL；
- ZIP；
- 固定 revision；
- quarantine；
- 自动扫描。

### T5.5 本地旁路发现

定期扫描本地 Skill 目录，与 SkillHub inventory 对比。

### T5.6 数字签名 POC（增强项）

对 Published Skill Package 生成公司签名，客户端验证。

---

## M6 — 试点与上线

### T6.1 选择试点团队

建议选择：

- Skill 数量中等；
- 有 DevOps/自动化 Skill；
- 能接受流程试点；
- 安全风险具有代表性。

### T6.2 导入历史 Skill

- 盘点；
- 去重；
- owner 认领；
- 分级；
- 扫描；
- 审核。

### T6.3 灰度规则

阶段 1：告警不阻断。

阶段 2：SkillHub 上架阻断。

阶段 3：L2/L3 Gerrit 合入阻断。

阶段 4：Runtime 可信源强制。

### T6.4 制度发布

正式发布：

- 《SKILL 安全管理策略》；
- 《Skill 开发规范》；
- 《Skill 安全审查 Checklist》；
- 《外部 Skill 引入流程》；
- 《Skill 漏洞应急处置流程》。

### T6.5 KPI Dashboard

- 覆盖率；
- 审核积压；
- 风险分布；
- 旁路数量；
- 漏洞处理时长。

---

## 任务优先级建议

### P0

- T0.1/T0.2/T0.4；
- T1.1/T1.2/T1.4；
- T2.1~T2.8；
- T3.1/T3.2/T3.5；
- T4.1~T4.6。

### P1

- 第二扫描器；
- Gerrit Label；
- 外部导入网关；
- Runtime digest 校验；
- reconciliation。

### P2

- 数字签名；
- 动态沙箱；
- 自动修复；
- 高级推荐/搜索。
