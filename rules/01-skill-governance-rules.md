# Skill 治理规则

本文件维护当前有效的治理细则，不记录方案形成过程。

## 1. 治理对象

- `SKILL.md` 是识别锚点，所在目录是 `Skill Root`。
- Skill Root 下纳入策略的所有文件共同构成 `Skill Package`。
- Skill Root 内任意受管控文件变化都必须进入发现和重新评估流程。
- 当前闭环治理 Gerrit 中的 Skill；未覆盖对象必须标记为未纳管，不能标记为可信。

## 2. 对象与身份

系统必须区分以下对象：

- `Canonical Skill`：逻辑上的同一项能力；
- `Skill Source`：一个具体仓库、分支、路径和名称来源；
- `Source Revision`：某个 Source 在一个 Git Revision 下的快照；
- `Content Version`：由完整包 SHA-256 标识的内容版本；
- `Scan Result`：扫描器针对内容版本的结果；
- `Review Record`：针对内容版本的审核结论。

Skill Source 使用以下组合标识：

```text
repository + branch + skill_path + skill_name
```

任一字段不同都先登记为独立 Source。多个 Source 可以关联到同一个 Canonical Skill，但必须：

- 保留全部 Source 和 Revision；
- 记录关联、拆分的操作者和时间；
- 支持误合并后的拆分；
- 不以物理删除、覆盖历史或仅按名称匹配完成合并。

## 3. 来源版本与内容版本

- `source_revision` 使用完整 Git commit/revision SHA。
- `skill_digest` 使用整个 Skill Package 的 SHA-256。
- Git Revision 用于来源追溯，Digest 用于内容去重和审查复用；二者不能互相替代。
- Source Revision、Content Version、扫描结果和审核记录均不可覆盖历史。

相同内容的结论只在以下键一致且原结论完整有效时复用：

```text
skill_digest + scanner_version + policy_version
```

复用后仍须为当前 Source Revision 建立独立追溯记录，并说明复用来源。扫描器或策略版本变化时，
必须重新评估是否仍可复用。

## 4. 发现与审查

- 上线前执行一次存量全量盘点；上线后使用 Gerrit 服务端事件增量发现，并定时对账补偿漏事件。
- 发现范围覆盖 Add、Modify、Delete、Rename、Copy，以及 Skill Root 内脚本、引用、配置等变化。
- 一个提交涉及多个 Skill 时，每个 Skill 独立生成 Source Revision 和任务。
- 连续 Patchset、重复事件和定时任务必须通过任务键实现幂等，旧版本结果不能标记为最新版本通过。
- 自动扫描器负责发现风险；CM 负责流程和结论确认；高风险、工具无法判断和例外升级给安全人员。

安全审查至少覆盖：指令风险、描述与行为一致性、脚本、文件与网络访问、凭据和环境变量、
工具/MCP 调用、外部 URL、依赖与安装、动态下载执行、混淆内容、敏感信息以及 Prompt Injection。

## 5. 状态与发布

底层应分别保存扫描、审核和 SkillHub 状态，不使用单一布尔值替代完整状态：

```text
Scan: NOT_SCANNED / PENDING / RUNNING / PASSED / FAILED / ERROR
Review: NOT_REVIEWED / PENDING / APPROVED / REJECTED / EXCEPTION / STALE
SkillHub: NOT_SYNCED / DRAFT / PUBLISHED / OFFLINE / REVOKED
```

推荐发布链路：

```text
Gerrit 发现 → 台账登记 → 冻结来源版本 → 计算内容版本 → 自动扫描 → 审核
→ 建立私密候选并提醒产品线 → 产品线确认和上传 → 核对版本 → 发布
```

平台、CM 和 SkillHub 管理员不得代替产品线自动公开上架。同步失败只影响同步状态，不能丢失
已完成的扫描和审核结论。
