# 《SKILL 安全管理策略》初稿

> 状态：Draft v0.2  
> 当前阶段：Gerrit 内部 Skill 治理  
> 基线日期：2026-08-25

## 1. 目的

建立公司内部 Skill 的统一识别、版本追溯、安全扫描、审核和 SkillHub 纳管机制，确保已经进入公司代码仓库的 Skill：

- 能被自动发现；
- 有明确来源和版本；
- Skill 内容变化能够被识别；
- 有可追溯的自动扫描与审核记录；
- 未通过安全审核的版本不能作为正式可信版本发布到 SkillHub。

## 2. 第一阶段适用范围

第一阶段仅纳管：

- 公司 Gerrit 代码仓库中存在 `SKILL.md` 的 Skill；
- `SKILL.md` 所在目录及其受管控子文件；
- 上述 Skill 的仓库来源、提交版本、内容版本、安全扫描、审核和 SkillHub 同步状态。

第一阶段暂不纳管：

- 开发者本地未进入 Gerrit 的 Skill；
- 公网 SkillHub / GitHub 上尚未进入公司代码仓库的 Skill；
- Runtime 侧强制可信源；
- 终端侧旁路检测；
- 外部 Skill 引入审批流程。

以上内容属于后续扩展范围，不属于当前策略首版的强制控制对象。

## 3. 术语定义

### 3.1 Skill Root

`SKILL.md` 所在目录。

### 3.2 Skill Package

Skill Root 下纳入公司策略管理的完整目录内容，包括但不限于：

- `SKILL.md`；
- scripts；
- references；
- assets；
- 配置文件；
- 依赖声明；
- 其他随 Skill 一起使用的文件。

`SKILL.md` 是 Skill 的识别锚点，但安全管理对象是完整 Skill Package。

### 3.3 Canonical Skill

逻辑上的同一个 Skill 能力。一个 Canonical Skill 可以存在多个代码仓库引用来源。

### 3.4 Skill Source

Skill 在一个具体代码来源中的存在形式。

第一阶段使用：

```text
repository + skill_path + skill_name
```

识别一个 Skill Source。

### 3.5 Source Revision

某个 Skill Source 在一次具体 Git commit/revision 下的版本快照。

### 3.6 Content Version / Skill Digest

对完整 Skill Package 做规范化后计算 SHA-256 得到 `skill_digest`，用于标识真实内容版本。

### 3.7 Scanner

对 Skill Package 进行自动安全检测的工具，可以是公司接入的扫描器，也可以是 SkillHub 自带扫描能力。

### 3.8 Review

CM 或指定 Reviewer 基于扫描结果、策略和必要人工确认形成的治理审核结论。

## 4. 核心管理原则

### 4.1 `SKILL.md` 定义边界，不限定变更触发文件

系统通过 `SKILL.md` 确定 Skill Root。

但 Skill Root 内任何受管控文件变化，都应被视为该 Skill 的一次潜在内容变更，而不是只关注 `SKILL.md` 是否变化。

### 4.2 Gerrit 服务端统一发现

Skill 检查必须由 Git/Gerrit 服务端统一触发，避免依赖开发者客户端环境。

服务端应识别：

- Add；
- Modify；
- Delete；
- Rename；
- Copy。

### 4.3 来源身份与逻辑身份分离

`repository + path + name` 只用于识别一个 Skill Source，不直接作为全局 Skill 唯一身份。

如果仓库、路径或名称任一不一致，先作为独立 Source 登记，后续再判断是否属于同一个 Canonical Skill。

多个 Source 合并时只做逻辑关联，不删除来源记录和历史。

### 4.4 Git 版本与内容版本分离

必须同时记录：

- commit/revision：用于 Git 来源追溯；
- SHA-256 digest：用于内容版本和安全审核。

核心规则：

> **Commit/Revision 是来源版本，Digest 是内容版本。**

同一个 digest 可能对应多个 commit，也可能对应多个 Skill Source。

### 4.5 安全审核绑定内容版本

扫描和审核结论应优先绑定：

```text
skill_digest + policy_version + scanner_version
```

不得仅依据 Skill 名称、路径或 commitid 判定“已经安全审核”。

### 4.6 自动扫描优先，CM 负责治理流程

CM 主要负责：

- 确认 Skill 被正确识别；
- 确认扫描任务执行完成；
- 查看和处理扫描结果；
- 维护待审核状态；
- 执行通过、驳回、整改或升级复核流程；
- 管理 SkillHub 纳管状态。

自动扫描工具负责提供安全风险判断依据。

对于高风险、扫描工具无法判断或策略例外的情况，可升级相关安全人员或专家处理。

### 4.7 SkillHub 是正式发布管理平台

发现新 Skill 后，首先进入公司治理数据库进行登记、扫描和审核。

推荐正式流程：

```text
Gerrit发现
 -> 登记
 -> 计算Digest
 -> 自动扫描
 -> CM/策略审核
 -> APPROVED
 -> SkillHub纳管/发布
```

如果 SkillHub 支持 Draft 状态，可在审核前创建 Draft 记录，但未通过公司安全策略前不得正式发布。

## 5. Skill 识别策略

### 5.1 Baseline 全量发现

系统上线前必须对纳管 Gerrit 仓库执行一次全量查找：

```text
仓库/正式分支
 -> 查找 SKILL.md
 -> 确定 Skill Root
 -> 获取 name
 -> 创建 Skill Source
 -> 创建当前 Source Revision
 -> 计算 skill_digest
 -> 进入扫描队列
```

### 5.2 增量发现

每次 Gerrit 服务端事件：

1. 获取 changed files；
2. 解析 A/M/D/R/C；
3. 对 old path/new path 定位受影响 Skill Root；
4. 识别 Skill Source；
5. 创建 Source Revision；
6. 获取完整 Skill Package；
7. 计算 SHA-256 digest；
8. 根据 digest 决定创建新 Content Version 或关联已有版本；
9. 触发扫描或复用已有扫描结果。

## 6. Skill Source 合并策略

### 6.1 初始登记

当以下任一字段不一致时，先认为是不同 Source：

- repository；
- skill_path；
- skill_name。

### 6.2 后续关联

多个 Source 可以关联到同一个 Canonical Skill。

关联时必须：

- 保留所有 Source；
- 保留所有 Revision；
- 保存关联操作人和时间；
- 支持取消错误关联；
- 不仅依赖名称自动合并。

可作为后续辅助判断的信号：

- digest 相同；
- `SKILL.md` name/description 一致；
- 来源仓库存在引用/复制关系；
- Owner/团队确认。

## 7. 版本管理策略

### 7.1 Source Revision

同一 Skill Source 的每个不同 commit/revision 均保留为独立 Source Revision。

即使两个 Revision 的 Skill 内容相同，也不能删除 Revision 历史。

### 7.2 Content Version

当两个 Revision 的 `skill_digest` 相同时，可关联到同一个 Content Version。

例如：

```text
commit A -> digest X
commit B -> digest Y
commit C -> digest Y
```

Source Revision 有 3 个，Content Version 只有 X、Y 两个。

### 7.3 Hash 算法

统一使用 SHA-256，不使用 MD5 作为安全版本或完整性标识。

## 8. 自动安全扫描策略

扫描可以通过以下方式触发：

- Gerrit 服务端发现新 digest 后立即触发；
- 定时批量扫描未完成或需重新评估的 Skill；
- SkillHub 自带安全扫描；
- 公司后续接入的其他 Scanner。

扫描结果至少记录：

- scanner_name；
- scanner_version；
- policy_version；
- skill_digest；
- 扫描开始/结束时间；
- status；
- risk_level / risk_score；
- findings；
- 原始报告引用。

同一 digest 已有有效扫描结果时，可以按策略复用；以下场景应重新扫描：

- Scanner 版本升级；
- Policy 版本升级；
- 原扫描失败或超时；
- 公司要求周期性复扫。

## 9. 第一阶段审查内容

至少关注：

1. `SKILL.md` 格式与指令内容；
2. Prompt Injection / Tool Poisoning；
3. 脚本执行能力；
4. Shell/Python/JS/PowerShell 等危险调用；
5. 网络访问；
6. 文件读写；
7. 凭据和环境变量访问；
8. MCP / Tool 调用；
9. 外部 URL；
10. 依赖和安装脚本；
11. 动态下载执行；
12. 混淆或隐藏 payload；
13. Skill 描述与实际行为是否一致；
14. 敏感信息和公司内部数据泄露风险。

## 10. 审核状态

不建议只保存“是否经过安全审查：是/否”。

### Scan Status

```text
NOT_SCANNED
PENDING
RUNNING
PASSED
FAILED
ERROR
```

### Review Status

```text
NOT_REVIEWED
PENDING
APPROVED
REJECTED
EXCEPTION
STALE
```

### SkillHub Status

```text
NOT_SYNCED
DRAFT
PUBLISHED
OFFLINE
REVOKED
```

前端可保留简化的“已审/未审”展示，但事实数据必须保留完整状态。

## 11. SkillHub 上架要求

Skill 正式进入 SkillHub 发布状态前至少满足：

- Source Revision 已登记；
- 当前 Skill Package digest 已计算；
- 必要自动扫描已经成功执行；
- 当前 digest 的审核状态满足公司策略；
- 无阻断级未处理风险；
- SkillHub 上传/发布操作可审计。

SkillHub 自带扫描可以作为额外安全检查，但不能替代公司侧版本和审核记录。

## 12. 变更与重新审核

### 新 commit，digest 不变

- 创建新的 Source Revision；
- 关联已有 Content Version；
- 可复用当前有效安全结论；
- 不重复创建内容版本。

### 新 commit，digest 变化

- 创建新的 Source Revision；
- 创建新的 Content Version；
- 自动触发扫描；
- 新内容不得直接继承旧 digest 的审核通过状态。

### Scanner/Policy 变化

即使 digest 不变，也可以触发重新扫描或重新评估。

## 13. 删除、移动和重命名

### 删除

删除 `SKILL.md` 或整个 Skill Root 时：

- Source Revision 仍保留历史；
- Skill Source 标记为 inactive/deleted；
- 不删除历史扫描和审核记录。

### Rename/Move

由于 `repository + path + name` 变化，首版可以产生新 Source，同时将旧 Source 标记为已迁移/结束。

后续通过 Canonical Skill 关联两者，避免丢失迁移历史。

## 14. 审计要求

至少保留：

- Skill Source 创建；
- Source Revision 创建；
- digest 创建/复用；
- Canonical Skill 关联/取消关联；
- 扫描开始/完成/失败；
- 审核通过/驳回/例外；
- SkillHub 同步、发布、下架；
- 管理员手工修改状态。

系统必须能够回答：

> 某个 SkillHub 已发布 Skill 来自哪个仓库、哪个目录、哪个 commit，对应哪个 digest，由哪个扫描器在什么策略版本下扫描，最终由谁批准。

## 15. 第一阶段不强制的能力

以下能力暂不作为 v0.2 强制要求：

- 外部 Skill 来源控制；
- Agent Runtime 强制内网 Registry；
- 数字签名；
- 动态沙箱执行；
- SBOM 强制；
- 复杂风险分级；
- 自动阻断所有 Gerrit 合入。

这些能力应在 Gerrit 内部 Skill 治理闭环稳定后再逐步扩展。

## 16. 第一阶段成功标准

1. 纳管 Gerrit 仓库可完成历史 Skill 全量盘点；
2. 新增/修改/删除/重命名 Skill 能稳定识别；
3. `SKILL.md` 未变化但 Skill Root 内其他文件变化仍可发现；
4. 所有 Source Revision 可追溯；
5. 所有内容版本均有 SHA-256 digest；
6. 同 digest 可以识别为同一 Content Version；
7. 自动扫描结果可入库并关联 digest；
8. CM 可以基于扫描结果完成审核；
9. Approved Skill 可以进入 iflytek SkillHub；
10. 未 Approved 的内容不能被误标记为正式可信版本。
