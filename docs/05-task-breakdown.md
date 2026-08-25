# SkillHub 安全管理项目任务拆分（WBS）

> 版本：v0.2

## 总体里程碑

| 里程碑 | 目标 | 建议周期 |
| --- | --- | --- |
| M0 | 固化 Skill 识别、版本和数据模型 | 1 周 |
| M1 | 搭建并验证 iflytek SkillHub | 1~2 周 |
| M2 | Gerrit Baseline + 服务端增量发现 MVP | 2~4 周 |
| M3 | Digest / Content Version / 自动扫描 | 2~4 周 |
| M4 | CM Review + SkillHub 自动纳管 | 2~4 周 |
| M5 | 对账、灰度和制度固化 | 2~3 周 |

---

## M0 — 规范与模型固化

### T0.1 Skill Root / Skill Package 规范

确认：

- `SKILL.md` 为识别锚点；
- `SKILL.md` 所在目录为 Skill Root；
- Skill Root 下哪些文件进入 Skill Package；
- 嵌套 `SKILL.md` 规则；
- symlink/LFS/submodule/二进制规则；
- 纳管 branch。

**输出**：Skill Package 规范 v0.2。

### T0.2 Skill Source 标识规范

首版固定：

```text
repository + skill_path + skill_name
```

明确：

- branch 是否进入唯一键；
- `skill_name` 的读取来源；
- name 缺失/冲突处理；
- rename/move 的 Source 生命周期。

### T0.3 版本模型

固化：

- Canonical Skill；
- Skill Source；
- Source Revision；
- Content Version；
- SHA-256 Digest；
- Scan Result；
- Review Record。

**输出**：ER/Schema v0.2。

### T0.4 Digest 规范

定义：

- path normalize；
- file order；
- SHA-256；
- file mode；
- 换行符；
- ignore list；
- LFS/symlink。

**验收**：同一 Git tree 在不同环境结果一致。

---

## M1 — iflytek SkillHub 搭建与接口验证

### T1.1 搭建测试环境

完成：

- 服务部署；
- 数据库；
- 基础账号/RBAC；
- 网络/证书；
- 备份方式确认。

### T1.2 验证 Skill 生命周期

重点确认：

- Register/Create；
- Draft；
- Review；
- Publish；
- Offline/Revoke；
- 版本不可变性。

### T1.3 验证 API/CLI

整理：

- Skill 创建 API；
- 上传/版本 API；
- 状态查询；
- Publish/Offline；
- Scanner API；
- 审计接口。

**输出**：SkillHub Adapter 接口清单。

### T1.4 验证自带 Scanner

确认：

- 扫描触发方式；
- 扫描结果格式；
- 风险等级；
- 是否能导出原始报告；
- 是否支持重新扫描；
- Scanner 版本如何获取。

### T1.5 决定同步时机

二选一并形成 ADR：

**模式 A**

```text
发现 -> 公司审核 -> SkillHub Register/Publish
```

**模式 B**

```text
发现 -> SkillHub Draft -> 公司审核 -> Publish
```

---

## M2 — Gerrit 自动发现 MVP

### T2.1 Baseline Scanner

- 获取纳管仓库列表；
- 获取纳管 branch；
- 全量搜索 `SKILL.md`；
- 建立 Skill Root；
- 创建 Skill Source；
- 创建当前 Source Revision。

### T2.2 Gerrit 服务端触发

实现服务端 Hook/Event/Plugin 接入。

至少获取：

- repository；
- branch；
- Change-Id；
- patchset；
- revision；
- parent revision。

### T2.3 Changed Files Resolver

支持：

- A；
- M；
- D；
- R；
- C；
- old_path/new_path。

### T2.4 Skill Root Resolver

实现：

```text
changed file -> 向上寻找 SKILL.md -> Skill Root
```

Delete/Rename 同时检查 parent revision old path。

### T2.5 Skill Source Resolver

根据：

```text
repository + skill_path + skill_name
```

创建或定位 Source。

### T2.6 Source Revision

每个受影响 Skill 创建不可变 Revision 记录。

### T2.7 Rename/Delete

实现：

- old Source inactive/moved/deleted；
- 新路径创建 Source；
- 保存 moved_from / relationship。

### T2.8 多 Skill Commit

确保单 commit 修改多个 Skill 时逐一生成 Revision。

---

## M3 — Content Version 与自动安全扫描

### T3.1 Skill Package Snapshot

- 获取完整目录；
- 生成 manifest；
- 文件大小/类型控制；
- 禁止扫描过程中执行脚本。

### T3.2 Digest Service

实现 SHA-256 Content Digest。

### T3.3 Content Version 去重

流程：

```text
new revision
 -> digest
 -> existing digest ? reuse : create
```

### T3.4 Scan Queue

新 Content Version 自动生成扫描任务。

### T3.5 Scanner Adapter

统一接口：

```text
scan(content_version, policy) -> normalized_result
```

首版可以先接：

- iflytek SkillHub 内置 Scanner；或
- 当前公司确定的一个自动扫描器。

第二扫描器作为 P1。

### T3.6 标准化 Scan Result / Finding

统一保存：

- scanner/version；
- policy/version；
- status；
- severity；
- evidence；
- report ref。

### T3.7 Scan 幂等与复用

任务 key：

```text
content_version
+ scanner
+ scanner_version
+ policy_version
+ mode
```

### T3.8 定时批量扫描

支持：

- 未扫描补偿；
- Scan Failed 重试；
- Scanner/Policy 升级后的批量重扫。

---

## M4 — CM Review 与 SkillHub 纳管

### T4.1 CM Review Queue

字段：

- Skill name；
- repository/path；
- latest commit；
- digest；
- scan status；
- risk；
- backlog age。

### T4.2 Review Detail

展示：

- `SKILL.md`；
- 目录文件；
- current revision；
- previous revision diff；
- previous approved content diff；
- Findings；
- Scanner 原始报告；
- 历史审核。

### T4.3 Review Action

支持：

- Approve；
- Reject；
- Request Changes；
- Escalate/Exception 预留。

### T4.4 安全结论复用

当新 Revision 指向已有 Content Version：

- 检查扫描结果是否有效；
- 检查 Review 是否有效；
- 复用时记录 audit event。

### T4.5 Canonical Skill 手工关联

后台支持：

- 推荐疑似相同 Source；
- Link to Canonical；
- Unlink；
- 保留操作历史。

### T4.6 SkillHub Sync Worker

实现：

```text
APPROVED -> Sync SkillHub
```

或根据 T1.5 结论实现 Draft + Publish。

### T4.7 SkillHub 状态同步

分离保存：

```text
review_status
skillhub_status
```

### T4.8 SkillHub 同步失败重试

- retry；
- dead letter；
- 告警；
- 手工重放。

---

## M5 — 对账、灰度与制度固化

### T5.1 Reconciliation

定时比较：

- Gerrit 最新 Skill Source；
- 数据库 Source；
- current revision；
- digest。

发现差异自动补偿或生成待办。

### T5.2 试点仓库

选择一批代表性仓库：

- 纯 Markdown Skill；
- 带 scripts；
- 多 Skill 仓库；
- rename/delete 历史；
- 多引用来源。

### T5.3 历史 Skill 批量扫描

Baseline 资产进入批量扫描与 CM Review。

### T5.4 指标

建立：

- Source 总数；
- Canonical Skill 数；
- Content Version 数；
- 未扫描数；
- 待审核数；
- SkillHub 未同步数；
- Reconciliation 差异数；
- 平均扫描时长；
- 平均审核时长。

### T5.5 正式策略文件

发布：

- 《SKILL 安全管理策略》；
- Skill Package 规范；
- CM 审查 Checklist；
- SkillHub 纳管流程；
- 异常/应急处理 SOP。

---

## 必测场景

- [ ] Baseline 发现已有 Skill
- [ ] 新增 `SKILL.md`
- [ ] 只修改 `scripts/a.py`
- [ ] 只修改 references
- [ ] 一个 commit 修改多个 Skill
- [ ] Skill Rename
- [ ] Skill Move
- [ ] 删除 `SKILL.md`
- [ ] Copy 一个 Skill
- [ ] 同 Source 新 commit 但 digest 不变
- [ ] 不同 Source digest 相同
- [ ] 重复 Gerrit Event
- [ ] 实时扫描与定时扫描同时触发
- [ ] Scanner Timeout
- [ ] SkillHub API 失败
- [ ] Canonical Skill 关联后再拆分

---

## 当前优先级

### P0

- T0.1~T0.4；
- T1.1~T1.5；
- T2.1~T2.8；
- T3.1~T3.5；
- T4.1~T4.3；
- T4.6；
- T5.1。

### P1

- 第二 Scanner；
- Scan/Review 复用优化；
- Canonical 推荐合并；
- SkillHub Draft 自动同步；
- Dashboard。

### 后续

- 外部 Skill 引入治理；
- Runtime 可信源；
- Gerrit Submit Block；
- 数字签名；
- 动态沙箱；
- 终端旁路检测。
