# Skill 批量安全审查与质量评分设计

> 文档版本：V1.0
>
> 文档状态：设计方案
>
> 适用范围：公司 Gerrit 代码仓库中已进入台账的 Skill
>
> 预计规模：100 多个代码仓库、数百个 Skill
>
> 输入形式：CSV 台账
>
> 本阶段归档目标：本地私密候选工作空间，后续由负责人手动同步到私密 Git 仓库

本文件说明存量 Skill 批量安全审查、质量评分和候选归档的执行规则。它不包含自动化脚本，不涉及 SkillHub 平台上架，也不改变 `docs/11-final-skill-security-management-framework.md` 中由产品线确认和上传的责任边界。

## 1. 设计目标

本方案需要解决以下问题：

- 从现有 CSV 台账稳定取得待审查 Skill；
- 同一仓库只下载一次，减少对 Gerrit 的重复访问；
- 同一 Skill 在多个分支中存在时，只选择最新有效内容进行常规审查，同时保留全部来源记录；
- 每次审查都绑定明确的 Git 版本和完整 Skill 内容；
- Cisco AI Skill Scanner 与 NVIDIA SkillSpector 并行执行静态检查；
- 使用公司内网模型和项目中的 AI 审查 Skill 进行第二层审查；
- 安全结论和质量得分分别计算，不能互相抵消；
- 只有满足要求的 Skill 才进入私密候选工作空间；
- 原始报告、统一结果、候选内容和处理日志能够完整追查；
- 单仓库完成后安全清理临时空间，再继续处理下一仓库；
- 中断后可以从失败项继续，不必重新执行整个批次。

## 2. 已确认的范围和边界

### 2.1 本阶段包含

- CSV 台账读取、校验、去重和批次冻结；
- Gerrit 仓库通过 SSH 下载；
- 跨分支最新 Skill 候选选择；
- 完整 Skill Package 快照和 SHA-256 内容校验值；
- Cisco AI Skill Scanner 静态检查；
- NVIDIA SkillSpector 静态检查；
- 基于公司内网模型的 AI 安全和质量审查；
- 统一问题清单、安全结论和质量得分；
- 需要人工确认的待办；
- 受限证据保存；
- 审查通过 Skill 的本地私密候选归档；
- 失败重试、断点继续和工作区清理。

### 2.2 本阶段不包含

- 自动修改原代码仓库中的 Skill；
- 执行被审查 Skill 自带的脚本或命令；
- 自动提交或推送私密候选 Git 仓库；
- 自动把 Skill 上架到 SkillHub；
- 代替产品线确认 Skill 是否应正式共享；
- 对公网模型发送任何 Skill 内容；
- 根据评分自动修复 Skill。

### 2.3 责任边界

自动化流程只负责发现、检查、评分、留证和生成私密候选。产品线仍是 Skill 的责任主体。当前阶段由负责人检查候选结果后，手动将候选目录全量更新到私密 Git 中转仓库。该中转仓库不是正式 SkillHub，也不向普通用户提供搜索和安装。

## 3. 核心原则

### 3.1 来源版本和内容版本同时保留

每个审查对象同时保存：

- **来源版本**：批次创建时冻结的分支 HEAD Commit，内部字段为 `source_revision`；
- **内容版本**：完整 Skill Package 计算得到的 SHA-256，字段为 `skill_digest`。

来源版本回答“从哪里取得”，内容版本回答“实际检查的内容是否相同”。不同来源版本可能得到相同内容校验值，相同内容可以复用有效结果，但来源记录不能删除。

### 3.2 CSV Commit 只作为台账提示

正式 CSV 中的 `latest_commitid` 进入系统后规范化为 `inventory_revision`。旧清单中的 `lasted_commited` 仍兼容，但不能与新字段同时出现。它与当前 `release/hooks/ref-update` 中 `skill_summary.latest_commitid` 的对应关系为：

```text
release 表字段 latest_commitid
        ↓ CSV 原样导出
CSV 字段 latest_commitid
        ↓ 批次读取后规范化
内部字段 inventory_revision
```

最终接受检查的 `source_revision` 不是直接照抄 `latest_commitid`，而是批次创建时从 Gerrit 冻结的目标分支 HEAD Commit。这样可以防止台账更新延迟或 Hook 参数取值错误导致审查旧内容。

### 3.3 检查完整 Skill Package

`SKILL.md` 只用于确认 Skill Root。检查和内容校验覆盖 Skill Root 下所有纳管文件，包括说明、脚本、配置、依赖、引用资料和资源文件。

### 3.4 两层审查缺一不可

第一层是 Cisco 和 SkillSpector 的并行静态检查；第二层是 AI 安全审查。任何一层未完成，都不能形成“安全通过”。AI 不能覆盖或降低静态工具发现的高风险问题。

### 3.5 安全与质量分开

安全回答“是否可以进入候选区”，质量回答“内容是否清楚、完整、可维护”。质量得分再高，也不能抵消严重或高风险问题。

### 3.6 不执行不可信内容

整个审查过程只读取和分析文件，不安装 Skill 依赖，不运行 Skill 自带脚本，不调用 Skill 声明的 MCP 或其他工具。

## 4. 总体流程

```text
┌──────────────────────┐
│ 读取并冻结 CSV 台账  │
└──────────┬───────────┘
           ↓
┌──────────────────────┐
│ 校验字段、状态和来源 │
└──────────┬───────────┘
           ↓
┌────────────────────────────┐
│ 按仓库和 Skill 路径建立    │
│ 跨分支候选视图             │
└──────────┬─────────────────┘
           ↓
┌────────────────────────────┐
│ 一次下载仓库并冻结各分支   │
│ HEAD，确定实际审查版本     │
└──────────┬─────────────────┘
           ↓
┌────────────────────────────┐
│ 导出完整 Skill 快照        │
│ 生成 Manifest 和 SHA-256   │
└──────────┬─────────────────┘
           ↓
┌─────────────────────────────────────────┐
│ 第一层：Cisco 与 SkillSpector 并行检查 │
└──────────┬──────────────────────────────┘
           ↓
┌─────────────────────────────────────────┐
│ 第二层：AI 安全审查与质量评分          │
└──────────┬──────────────────────────────┘
           ↓
┌────────────────────────────┐
│ 统一问题、去重、风险判定   │
│ 生成独立质量得分           │
└──────────┬─────────────────┘
           ↓
     ┌─────┴──────────────┐
     │                    │
     ↓                    ↓
┌──────────────┐  ┌─────────────────────┐
│ 通过         │  │ 阻断 / 待人工确认  │
│ 生成私密候选 │  │ 只保存证据和待办   │
└──────┬───────┘  └──────────┬──────────┘
       └───────────┬─────────┘
                   ↓
┌────────────────────────────┐
│ 验证结果已落盘后清理仓库   │
│ 临时工作区，继续下一仓库   │
└────────────────────────────┘
```

## 5. CSV 输入设计

### 5.1 输入字段

CSV 保持用户当前字段名称，不要求在源文件中改名。

| CSV 字段 | 是否必填 | 内部字段 | 使用规则 |
|---|---:|---|---|
| `skill_name` | 是 | `inventory_skill_name` | 台账名称，用于展示和一致性校验，不单独决定跨分支合并 |
| `repo_name` | 是 | `repository` | Gerrit 项目名，通过受控配置映射到 SSH 地址 |
| `branch` | 是 | `source_branch` | 兼容普通分支名和 `refs/heads/` 格式，内部统一为普通分支名 |
| `skill_path` | 是 | `normalized_skill_path` | 仓库内 Skill Root 路径，必须能定位到 `SKILL.md` |
| `latest_commitid` | 是 | `inventory_revision` | release 表原始字段；仅作台账版本提示和一致性检查。旧字段 `lasted_commited` 继续兼容 |
| `security_reviewed` | 是 | `inventory_review_hint` | 只保留为旧台账上下文；首次统一审查不据此跳过，也不作为正式审查证据 |
| `status` | 是 | `inventory_status` | 判断来源是否有效；未知状态进入数据待确认 |
| `skill_id` | 是 | 追溯字段 | 保留台账标识，不代替 Source 身份 |
| `update_time` | 是 | 追溯字段 | 保留台账更新时间，不用于替代 Git 时间 |
| `history_id` | 是 | 追溯字段 | 保留历史记录标识 |

### 5.2 派生字段

批次处理时补充以下字段：

```text
batch_id
source_row_id
repository
source_branch
resolved_branch_head
skill_last_change_revision
skill_last_change_time
source_revision
normalized_skill_path
resolved_skill_name
skill_digest
source_selection_status
```

其中：

- `source_row_id` 是原始 CSV 行的稳定编号，便于回查；
- `resolved_branch_head` 是批次创建时冻结的分支 HEAD；
- `skill_last_change_revision` 是该分支中指定路径最后一次变化的 Commit；
- `source_revision` 是实际用于导出 Skill 快照的 `resolved_branch_head`；
- `resolved_skill_name` 从 `SKILL.md` 解析，不能静默覆盖 CSV 名称；
- `source_selection_status` 说明该来源是被选中、被更新分支替代、发生冲突还是数据无效。

### 5.3 CSV 校验规则

读取后必须完成以下校验：

1. 文件能够按 UTF-8 正常解析，表头只出现一次；
2. 正式清单的十个规定字段全部存在且必填值不为空；旧七列格式仍兼容；
3. `repo_name` 能通过受控配置解析到唯一 Gerrit SSH 仓库；
4. `branch` 在远端存在；
5. `skill_path` 是仓库内相对路径，不允许绝对路径、`..` 越界、空字节或未规范化分隔符；
6. 根目录 Skill 的 `/` 写法在内部统一为 `.`；
7. `<skill_path>/SKILL.md` 在冻结版本中存在；
8. `latest_commitid` 或兼容字段 `lasted_commited` 能在对应仓库解析为 Commit；
9. `skill_name` 与 `SKILL.md` 中的名称不一致时记录 `SKILL_NAME_MISMATCH`，不自行改写源数据；
10. 完全重复的 CSV 行只创建一个执行任务，但所有原始行号都要保留；
11. 同一来源键出现互相矛盾的状态或 Commit 时，标记 `INPUT_CONFLICT`；
12. `status` 的允许值和映射表在运行配置中固定，未知值不得自动按有效处理。

### 5.4 `security_reviewed` 的使用方式

首次统一审查时，建议对当前有效 Skill 全量重审，不因为 CSV 中已有“是”而跳过。以后增量批次中，只有能够找到以下完整证据时才可以复用：

```text
skill_digest
+ scanner_name / scanner_version
+ scan_mode
+ policy_version
+ AI 审查 Skill 版本
+ AI 模型标识
```

仅有 `security_reviewed=是` 但没有这些证据时，仍视为未完成本轮正式审查。

## 6. 跨分支最新 Skill 选择规则

用户当前要求是：同一仓库不同分支中的同一 Skill，只关注最新一次变化后的内容。该要求只影响本批次的审查目标选择，不删除台账中的分支来源事实。

### 6.1 两种身份分别处理

来源事实身份继续使用：

```text
repo_name + branch + normalized_skill_path + skill_name
```

跨分支审查目标使用：

```text
repo_name + normalized_skill_path
```

`skill_name` 只用于展示和一致性校验，不用于把不同路径合并。同名但路径不同的两个 Skill 必须分别扫描。

### 6.2 选择算法

对每一个 `(repo_name, normalized_skill_path)` 分组执行：

1. 保留组内所有 CSV 来源行；
2. 下载或更新该仓库的一份只读镜像；
3. 在批次创建时读取并冻结每个候选分支的 HEAD Commit；
4. 在每个冻结 HEAD 上确认 `SKILL.md` 和 Skill Root 存在；
5. 取得该分支中 `skill_path` 最近一次发生变化的 Commit 和 Commit 时间；
6. 比较的是 Skill 路径最近变化时间，不能比较 Commit SHA 字符串，也不能用 CSV 行顺序；
7. 最近变化时间最大的分支候选成为常规审查目标；
8. 实际快照从该分支冻结的 HEAD 导出，而不是从会继续变化的分支名导出；
9. 其他较旧分支来源标记为 `SKIPPED_SUPERSEDED_BRANCH`，但仍保留来源、版本和选择依据；
10. 将 `inventory_revision` 与实际分支 HEAD、路径最近变化 Commit进行核对，不一致时记录台账差异。

选择过程可以表示为：

```text
同仓库 + 同 Skill 路径
        ↓
取得每个分支的冻结 HEAD
        ↓
取得路径在各分支的最近变化时间
        ↓
选择最近变化时间最大者
        ↓
从选中分支的冻结 HEAD 导出完整 Skill
```

### 6.3 相同时间和分支分叉

当两个分支的 Skill 最近变化时间相同时：

- 如果两个快照的 `skill_digest` 相同，只扫描一次，两个来源共同关联该内容版本；
- 如果 `skill_digest` 不同，不静默选择其中一个，两个版本都进入扫描并标记 `BRANCH_CONTENT_CONFLICT`；
- 冲突版本即使分别通过，也需人工确认哪一个进入私密候选区。

若 Git 能确认一个候选版本是另一个候选版本的后代，可把后代关系作为辅助信息；不能仅凭分支名称判断新旧。

### 6.4 台账版本不一致

以下情况记录为 `STALE_INVENTORY`：

- `latest_commitid`（或兼容字段 `lasted_commited`）无法解析；
- CSV Commit 与远端分支或该 Skill 路径没有合理关联；
- CSV Commit 明显早于当前路径最近变化 Commit；
- CSV 状态声称有效，但冻结 HEAD 中已经没有该 Skill；
- CSV 中的分支、路径和实际仓库内容不一致。

发生 `STALE_INVENTORY` 时不得静默覆盖 CSV 后继续标记安全通过。默认做法是保留差异证据、阻断该项并修正台账；如果经负责人明确确认使用远端冻结 HEAD，必须把修正原因和操作人写入批次记录。

### 6.5 与当前 release Hook 的衔接

当前 `release/hooks/ref-update`：

- 表字段使用 `latest_commitid`；
- 正式 CSV 原样使用 `latest_commitid`；旧导出中的 `lasted_commited` 仅作为兼容字段；
- Hook 中使用 `gerritCommitId = sys.argv[10]`。按照 [Gerrit 官方 `ref-update` 参数顺序](https://gerrit.googlesource.com/plugins/hooks/+/HEAD/src/main/resources/Documentation/hooks.md)，该位置通常是 `oldrev`，更新后的 `newrev` 通常位于 `sys.argv[12]`；除非生产部署存在额外包装，否则当前 release 代码记录的可能是更新前版本；
- release 表本身没有 `status` 字段，因此 CSV 的 `status` 来源和值域必须单独固定，不能假定它由当前 Hook 维护；
- 批量审查不能仅依赖这一字段判断最新内容，必须通过远端分支 HEAD 和 Skill 路径最近变化记录再次核实。

在开始全量审查前，应抽取新增、修改和连续提交样本，确认 `latest_commitid` 最终指向预期版本。该检查属于本方案的输入验收条件。

当前 Hook 还包含直接写入代码的访问凭据，并使用字符串拼接后交给 `os.popen` 执行 Git 命令。审查流水线不能沿用这种凭据和命令调用方式；已经写入代码且仍有效的凭据应先轮换，运行身份改为受控注入，命令参数必须作为独立参数传递。本设计只记录这一输入风险，不在本文件中修改 Hook。

## 7. 按仓库处理的工作模型

### 7.1 仓库级下载

处理单位首先按 `repo_name` 分组。每个仓库在一个批次内只建立一份只读镜像或对象缓存，所有相关分支和 Commit 从这一份仓库对象中解析。

这样可以避免：

- 同一仓库因多个 Skill 被重复下载；
- 不同 Skill 使用不同时间下载到变化中的分支；
- 数百个 Skill 对 Gerrit 产生不必要压力。

### 7.2 Skill 级不可变快照

同一仓库中的不同 Skill 可能来自不同分支，因而可能对应不同的 `source_revision`。不能把所有 Skill 强行放在一个可变 checkout 中检查。

推荐模型：

```text
一个仓库只读镜像
├── revision A
│   ├── Skill 1 快照
│   └── Skill 2 快照
├── revision B
│   └── Skill 3 快照
└── revision C
    └── 分支冲突版本快照
```

每个 Skill 快照放入独立、只读的工作目录。扫描器看到的路径只包含当前 Skill Package，不能通过相邻目录读取其他仓库内容。

### 7.3 不使用普通目录复制作为事实来源

普通复制可能丢失文件权限、符号链接、Git LFS 状态等信息。快照应从明确的 Git tree 导出，并同时生成文件清单。快照生成后立即计算 `skill_digest`，后续所有扫描器和 AI 审查都必须核对同一个 Digest。

### 7.4 特殊内容处理

| 内容类型 | 处理规则 |
|---|---|
| 符号链接 | 快照时不跟随链接；记录链接目标。指向 Skill Root 外部时阻断自动通过 |
| Git LFS | 必须取得真实对象；只有指针文件时标记检查不完整 |
| Git 子模块 | 不自动执行或信任子模块；记录版本，未纳入检查时标记检查不完整 |
| 压缩包和二进制 | 保存摘要和类型；扫描器无法覆盖时进入人工确认 |
| 超大文件 | 按固定大小限制处理，超过限制不能静默跳过 |
| 隐藏文件 | 默认纳入 Skill Package 和 Digest |
| Skill Root 外部引用 | 不复制到包内；记录越界引用并进入人工确认 |
| 嵌套 `SKILL.md` | 外层和内层分别作为 Skill Root，避免同一文件被错误归属 |

## 8. Skill Package 和 Digest

### 8.1 Package Manifest

每个快照生成规范化 Manifest，至少包含：

```text
relative_path
file_type
file_mode
file_size
file_sha256
symlink_target（如有）
```

Manifest 中的路径统一使用 `/`，按路径排序。内容校验值使用整个规范化 Manifest 再计算 SHA-256。

### 8.2 Digest 范围

`skill_digest` 覆盖完整纳管 Skill Package，不只覆盖 `SKILL.md`。换行符按 Git blob 原始内容处理，可执行权限进入 Manifest。二进制同样计算文件 SHA-256。

### 8.3 内容复用

相同 `skill_digest` 可以复用已经存在的扫描结果，但必须同时满足：

- Cisco 工具版本和配置版本相同；
- SkillSpector 工具版本和配置版本相同；
- 公司审查规则版本相同；
- AI 审查 Skill 版本相同；
- AI 模型标识和输出格式版本相同；
- 原结果完整且未被撤销；
- 当前策略没有要求强制重扫。

每次复用都生成一条 `RESULT_REUSED` 记录，指向原始证据，不能只在结果表中复制“通过”。

## 9. 两层审查设计

### 9.1 审查前检查

进入扫描前先确认：

- `SKILL.md` 存在且可以读取；
- Skill 根路径未越界；
- 快照 Manifest 和 Digest 已生成；
- 快照在扫描开始前后 Digest 一致；
- 文件数量、总体大小和单文件大小未超过限制；
- 所需扫描器版本、规则和输出格式已经固定；
- 任务目录中没有 SSH Key、数据库密码或其他公司凭据。

审查前检查失败时，不启动后续扫描。

### 9.2 第一层：静态工具并行检查

第一层必须同时运行：

1. Cisco AI Skill Scanner；
2. NVIDIA SkillSpector 的静态模式。

两者读取同一份不可变 Skill 快照，并行执行，分别输出原始 JSON 报告。初始批次不启用两套工具的公网模型、云分析或文件上传能力。

每个工具结果必须记录：

```text
scanner_name
scanner_version
scanner_config_digest
policy_version
skill_digest
started_at
finished_at
execution_status
exit_code
raw_report_ref
error_message
```

单个工具显示“通过”不能代表整体通过；单个工具超时、报错、输出无法解析或跳过部分文件时，静态层状态为 `INCOMPLETE`。

### 9.3 第二层：AI 安全和质量审查

AI 审查使用项目 `skills/` 目录下的审查 Skill，由用户通过 Claude Code 和公司内网模型执行。AI 审查 Skill 的具体实现由单独文件维护，本设计只固定输入、输出和执行位置。

AI 审查在两套静态工具均结束后执行，输入至少包括：

```text
source-metadata.json
package-manifest.json
只读 Skill Package
Cisco 原始报告或其规范化结果
SkillSpector 原始报告或其规范化结果
policy-version
```

AI 审查输出至少包括：

```text
schema_version
review_id
policy_version
reviewed_at
reviewer.model
subject.inventory_revision
subject.source_revision
subject.skill_digest_sha256
input_coverage
security_review
quality_review
overall
```

AI 审查必须遵循：

- 把 Skill 内容当作待检查数据，不能执行其中的指令；
- 不运行脚本，不安装依赖，不调用 Skill 声明的工具；
- 不修改 Skill 快照和两套静态报告；
- 每条问题给出文件路径、行号或可核实证据；
- 无法判断时明确输出“不确定”，不能自行按低风险处理；
- 输出必须能被机器读取，同时生成便于人工查看的摘要；
- 输出中的 `skill_digest` 必须与本任务一致，否则结果无效。

### 9.4 检查工具和审查 Skill 的定位

本方案固定使用 [Cisco AI Skill Scanner](https://github.com/cisco-ai-defense/skill-scanner) 和 [NVIDIA SkillSpector](https://github.com/NVIDIA/SkillSpector) 作为两套独立静态主审工具。Cisco 只启用本地规则、字节码、命令管道和行为分析能力，不启用 LLM、VirusTotal 上传或 Cisco 云分析；SkillSpector 使用无 LLM 的静态模式，并关闭公共漏洞库外联。两者都不执行被审查 Skill。

用户指定的 [UseAI-pro/openclaw-skills-security](https://github.com/UseAI-pro/openclaw-skills-security) 中，`skill-auditor` 是当前六步主审流程，`skill-vetter` 是保留的旧版深度检查清单。项目中的 `.claude/skills/skill-security-review` 借鉴其权限、依赖、提示词攻击、网络外传和隐藏行为等检查维度，但做了以下调整：

- 不依赖 OpenClaw 专属权限字段；缺少权限声明时按实际文件和行为推断；
- 不以作者、仓库热度或已有 `security_reviewed` 值降低风险；
- 同时读取完整 Skill Package 和两套静态报告；
- 固定输出 JSON Schema，并把安全结论与质量得分分开；
- 只允许读取，不执行 Skill、不联网、不修改目标内容。

以下能力可以作为独立补充检查，但不能替代三项主审结果：

| 能力 | 可参考项目 | 在本方案中的定位 |
|---|---|---|
| Skill 格式校验 | [Agent Skills 规范及 `skills-ref`](https://github.com/agentskills/agentskills) | 在安全扫描前发现 `SKILL.md` 格式、名称和目录问题 |
| 凭据检查 | [Gitleaks](https://github.com/gitleaks/gitleaks) | 补充检查硬编码密码、Token 和私钥，报告必须脱敏 |
| 依赖漏洞 | [OSV-Scanner](https://github.com/google/osv-scanner) 或 [Trivy](https://github.com/aquasecurity/trivy) | 检查依赖锁文件和已知漏洞；公司环境中使用离线数据或批准的内网源 |
| 第二套 AI 安全意见 | [腾讯 AI-Infra-Guard Skill Scan](https://github.com/Tencent/AI-Infra-Guard/tree/main/skill-scan) | 仅在公司内网模型和隔离环境中作为补充意见 |
| AI 审查清单 | [腾讯 edgeone-skill-scanner](https://github.com/Tencent/AI-Infra-Guard/blob/main/skills/edgeone-skill-scanner/SKILL.md) | 参考说明与行为一致性、权限和数据使用检查，不直接作为最终门禁 |
| Skill 静态质量检查 | [OpenAI evaluate-skill](https://github.com/openai/plugins/blob/main/plugins/plugin-eval/skills/evaluate-skill/SKILL.md)、[Anthropic skill-creator](https://github.com/anthropics/skills/blob/main/skills/skill-creator/SKILL.md) | 参考触发准确性、说明清晰度、结构和可维护性维度 |
| 实际效果评测 | [NVIDIA SkillEvaluator](https://docs.nvidia.com/skills/skillevaluator/) | 需要测试用例和受控执行环境，不属于本阶段只读安全审查 |

任何补充工具都应以独立适配方式接入，保留原始报告，只能补充统一问题清单，不能覆盖 Cisco、SkillSpector 或 AI 的原始结论。

## 10. 统一问题清单

### 10.1 标准问题字段

不同工具的问题统一为：

```text
finding_id
skill_digest
source_scanner
source_rule_id
category
severity
title
description
file_path
start_line
end_line
evidence_summary
recommendation
confidence
fingerprint
status
```

`evidence_summary` 必须脱敏，不直接复制完整密钥、令牌或大段源码。

### 10.2 风险等级映射

各工具自身等级统一映射为：

```text
CRITICAL  严重
HIGH      高
MEDIUM    中
LOW       低
INFO      提示
UNKNOWN   无法判断
```

原始等级必须保留。统一映射只能用于排序和准入判断，不能改写工具原报告。

### 10.3 去重规则

Cisco、SkillSpector 和 AI 可能发现同一个问题。统一层按以下信息生成问题指纹：

```text
问题类别 + 规范化文件路径 + 位置 + 规范化证据摘要
```

指纹相同的问题在汇总报告中合并展示，但保留所有来源工具和各自原始严重级别。合并后采用最高严重级别，不能通过平均等级降低风险。

## 11. 安全结论

### 11.1 安全结论状态

统一安全结论只有以下四种：

| 状态 | 含义 | 是否进入候选区 |
|---|---|---:|
| `PASS` | 两层审查完整，且没有需要阻断或人工确认的问题 | 是 |
| `REVIEW_REQUIRED` | 存在高、中、不确定、特殊文件或分支内容冲突 | 否，人工确认后重新判定 |
| `BLOCKED` | 存在严重风险、明确恶意行为或越界内容 | 否 |
| `INCOMPLETE` | 工具失败、超时、报告缺失、内容未覆盖或 Digest 不一致 | 否 |

### 11.2 默认判定规则

- AI Skill 输出与统一结论的映射为：`BLOCK → BLOCKED`、`REVIEW_REQUIRED → REVIEW_REQUIRED`、`INCOMPLETE → INCOMPLETE`、`PASS → PASS`；
- 任一严重问题：`BLOCKED`；
- 任一高风险问题：`REVIEW_REQUIRED`；
- 首次全量批次中的中风险问题：`REVIEW_REQUIRED`；
- 只有低风险和提示，且两层审查完整：可以 `PASS`；
- 任一工具未执行成功或输出无法验证：`INCOMPLETE`；
- AI 明确表示无法判断：`REVIEW_REQUIRED`；
- 分支相同时间但内容不同：`REVIEW_REQUIRED`；
- `STALE_INVENTORY` 未完成确认：`INCOMPLETE`；
- 质量得分不参与上述安全准入计算。

### 11.3 人工结论

人工确认只能在完整自动审查基础上形成，必须保存：

```text
reviewer
reviewer_role
decision
reason
accepted_findings
required_actions
valid_until（如有）
reviewed_at
policy_version
```

人工确认不能删除原问题，只能更新问题处理状态。例外通过必须说明范围和有效期。

## 12. 独立质量评分

### 12.1 评分定义

质量分取值为 0 至 100，越高表示说明更清楚、内容更完整、维护成本更低。首版质量分用于排序和整改参考，不表示实际业务效果，也不能替代安全结论。

### 12.2 评分维度

| 维度 | 分值 | 主要检查内容 |
|---|---:|---|
| 目的与触发条件 | 20 | 名称、用途、触发条件、输入输出和不适用范围是否明确 |
| 指令清晰度 | 25 | 执行步骤、前后顺序、预期结果和说明是否完整一致 |
| 范围与权限匹配 | 15 | 文件、网络、命令、账号、数据和工具权限是否与用途匹配 |
| 稳定性与边界处理 | 20 | 缺失输入、异常、超时、不支持内容和停止条件是否说明 |
| 可维护性与可验证性 | 20 | 文件组织、依赖、重复内容、示例、测试依据和结果验证是否清楚 |
| **合计** | **100** |  |

每个维度必须同时输出分数、扣分原因和证据。没有证据的主观评分不得计入正式质量分。

### 12.3 建议质量等级

| 得分 | 等级 | 建议处理 |
|---:|---|---|
| 90–100 | 优秀 | 内容清楚，可作为私密候选 |
| 85–89 | 良好 | 少量优化后可长期维护 |
| 70–84 | 合格 | 可以进入私密候选，但应保留改进项 |
| 0–69 | 不合格 | 不进入候选区，先补齐基本质量 |

首版候选质量门槛建议为 70 分。该门槛在真实样本试运行后可以通过新的评分规则版本调整，不能回写旧批次得分。

AI Skill 中 `quality_review.verdict=PASS` 对应 70–100 分，`FAIL` 对应 0–69 分，`INCOMPLETE` 表示无法形成有效分数。只有安全结论为 `PASS` 且质量结论为 `PASS` 时，AI 层才可以给出 `APPROVE_CANDIDATE`。

### 12.4 质量分边界

当前静态和 AI 阅读能够评价规范、清晰度和可维护性，但不能证明 Skill 在真实任务中一定有效。若以后需要评价实际效果，应单独准备任务样例和预期结果，在受控环境中做功能评测，不纳入本阶段默认流程。

## 13. 状态设计

一个 Skill 不能只保存“已审查/未审查”。至少分别保存以下状态。

### 13.1 来源选择状态

```text
RECEIVED
VALIDATING
SELECTED
SKIPPED_SUPERSEDED_BRANCH
BRANCH_CONTENT_CONFLICT
STALE_INVENTORY
INPUT_INVALID
SOURCE_UNAVAILABLE
```

### 13.2 快照状态

```text
NOT_CREATED
CREATING
READY
FAILED
```

### 13.3 静态检查状态

```text
NOT_STARTED
PENDING
RUNNING
COMPLETED
INCOMPLETE
ERROR
TIMEOUT
```

Cisco 和 SkillSpector 分别保存自己的状态，静态层再保存一个汇总状态。

### 13.4 AI 审查状态

```text
NOT_STARTED
WAITING_FOR_MANUAL_EXECUTION
RUNNING
COMPLETED
INVALID_OUTPUT
ERROR
```

### 13.5 候选归档状态

```text
NOT_ELIGIBLE
READY_TO_EXPORT
EXPORTED_LOCAL
VERIFIED
MANUAL_SYNC_PENDING
MANUALLY_SYNCED
FAILED
```

### 13.6 批次状态

```text
CREATED
VALIDATING
READY
RUNNING
WAITING_FOR_AI_REVIEW
WAITING_FOR_MANUAL_REVIEW
PARTIALLY_COMPLETED
COMPLETED
FAILED
```

批次中部分 Skill 失败时，已完成 Skill 的结果仍然有效，批次进入 `PARTIALLY_COMPLETED`，不能把整个批次简单重置后重复扫描。

## 14. 重试、幂等和断点继续

### 14.1 任务唯一键

单工具扫描任务建议使用：

```text
skill_digest
+ scanner_name
+ scanner_version
+ scanner_config_digest
+ policy_version
+ scan_mode
```

AI 审查任务再增加：

```text
AI 审查 Skill 版本
+ model_identifier
+ review_schema_version
```

相同任务重复触发时返回已有完整结果，不重新创建互相矛盾的报告。

### 14.2 可重试错误

以下情况可以自动或人工重试：

- Gerrit 临时连接失败；
- 扫描器进程异常退出；
- 临时资源不足；
- AI 审查执行中断；
- 结果落盘暂时失败。

每个阶段默认最多执行 3 次，包括首次执行。每次重试保存次数、原因和时间。超过次数后进入 `ERROR` 或 `INCOMPLETE`，等待人工处理。

### 14.3 不应直接重试的错误

- CSV 字段缺失或路径越界；
- Commit 不存在；
- `SKILL.md` 不存在；
- LFS 对象或子模块内容长期缺失；
- 报告 Digest 与 Skill Digest 不一致；
- 分支内容冲突；
- 明确安全阻断问题。

这些问题需要修正数据、内容或策略后创建新任务，不能通过反复重试改变结论。

### 14.4 断点继续

每个阶段完成后写入阶段完成标记和输出摘要。重新启动批次时，只执行没有完整结果或需要重试的阶段。仓库临时目录已经清理时，可以根据冻结的 `source_revision` 重新建立相同快照，并用 Digest 验证内容一致。

## 15. 工作空间设计

### 15.1 区域分离

必须区分以下区域：

```text
batch-control/       批次清单、状态和脱敏汇总
repo-cache/          当前批次的只读仓库镜像
skill-work/          单 Skill 临时快照和扫描工作区
restricted-evidence/ 受限证据区
private-candidates/  私密候选区
```

`restricted-evidence` 和 `private-candidates` 不能混用。原始报告可能包含源码片段、疑似密钥、内部地址或模型分析内容，只能进入受限证据区。

### 15.2 受限证据区

每个 Skill 的证据至少包括：

```text
source-metadata.json
package-manifest.json
package-digest.txt
scanner-versions.json
cisco/raw-report.json
skillspector/raw-report.json
ai-review/raw-review.json
normalized/findings.json
normalized/security-result.json
normalized/quality-result.json
summary/review-summary.md
execution/events.jsonl
```

原始报告应限制访问并按公司要求保存。发现疑似密码或令牌时，汇总报告只能显示脱敏值，不能把完整内容复制到候选 Git 工作空间。

### 15.3 私密候选区

只有同时满足以下条件的 Skill 才进入私密候选区：

- 安全结论为 `PASS`；
- 质量得分达到当前门槛；
- 两套静态报告和 AI 报告完整；
- 快照 Digest 与最终结果完全一致；
- 没有未确认的分支内容冲突；
- 来源台账没有未解决的 `STALE_INVENTORY`；
- 内容不含被策略禁止进入候选仓库的敏感信息。

建议候选目录结构：

```text
private-candidates/
└── <repo-slug>/
    └── <skill-path-slug>/
        └── <skill-digest>/
            ├── package/                 完整 Skill 内容
            ├── source-manifest.json     仓库、分支、Commit、路径、Digest
            └── review-summary.json      脱敏结论和质量分
```

目录不能仅以 `skill_name` 命名，避免不同仓库或同名 Skill 相互覆盖。

本阶段只生成本地候选工作空间，不自动执行 Git Commit 或 Push。用户确认后，再将候选内容手动全量同步到私密 SkillHub Git 中转仓库。

### 15.4 私密候选 Git 仓库要求

手动同步时仍应遵守：

- 仓库保持私密并按产品线或管理角色限制权限；
- 不放入完整原始扫描报告；
- 不放入未通过、待确认或检查不完整的 Skill；
- 每个 Skill 保留来源 Commit 和 Digest；
- 不把该仓库称为正式 SkillHub；
- 不从该仓库自动公开发布；
- 后续产品线确认和正式平台建设不受本阶段自动化替代。

## 16. 工作区清理

### 16.1 清理前置条件

只有同时完成以下检查后才可以清理某仓库工作区：

1. 仓库内所有选中 Skill 已有明确终态；
2. 原始报告已进入受限证据区；
3. 统一结果、事件日志和状态已经落盘；
4. 候选 Skill 已导出并重新核对 Digest；
5. 结果索引可以从 `batch_id + task_id` 查询；
6. 保存区写入失败的任务已经留有恢复信息。

### 16.2 清理对象

可以清理：

- 仓库只读镜像或本批次缓存；
- Skill 临时快照；
- 扫描器临时文件；
- AI 审查临时输入副本。

不能清理：

- 批次输入快照；
- 受限证据；
- 私密候选内容；
- 统一结果；
- 人工审查记录；
- 失败原因和重试记录。

### 16.3 失败现场

无法解释的工具错误、结果损坏或 Digest 不一致时，相关工作区进入隔离保留状态，并设置明确保留期限。确认不再需要后才能清理，不能因为继续处理下一仓库而立即删除证据。

## 17. 推荐执行顺序

### 17.1 一次批次的顺序

```text
步骤 1  冻结原始 CSV，生成 batch_id 和每行 source_row_id
步骤 2  校验字段、状态、仓库、分支、Commit 和路径
步骤 3  按 repo_name 分组，建立仓库处理队列
步骤 4  下载一个仓库的只读镜像并冻结候选分支 HEAD
步骤 5  按 repo_name + normalized_skill_path 选择跨分支候选
步骤 6  对选中 revision 分组，逐个导出独立 Skill 快照
步骤 7  生成 Manifest 和 SHA-256 Digest，判断是否可复用结果
步骤 8  Cisco 与 SkillSpector 并行执行静态检查
步骤 9  验证两套报告完整性，生成 AI 审查输入包
步骤 10 用户通过 Claude Code 执行项目中的 AI 审查 Skill
步骤 11 校验 AI 输出的 Digest、格式和证据
步骤 12 汇总并去重问题，生成安全结论和独立质量得分
步骤 13 将需要确认的问题加入人工待办
步骤 14 将满足条件的 Skill 导出到私密候选区
步骤 15 验证证据和候选内容完整后清理仓库临时空间
步骤 16 继续下一个仓库
步骤 17 汇总批次结果、失败项、待确认项和候选清单
```

### 17.2 Claude Code 执行 AI 审查的顺序

用户通过 Claude Code 执行时，每个 Skill 必须遵守：

```text
确认 Cisco 报告已完成
        ↓
确认 SkillSpector 报告已完成
        ↓
确认两个报告与 Skill Digest 一致
        ↓
调用项目 skills/ 下的 AI 审查 Skill
        ↓
AI 读取完整 Skill + 两套静态报告
        ↓
输出 AI 安全结论 + 质量各维度评分
        ↓
校验输出格式、Digest 和证据
        ↓
进入统一汇总
```

不建议在静态工具尚未完成时先运行 AI 审查，否则 AI 无法对工具结果做交叉确认，且失败重跑容易造成不同输入版本的结论混用。

项目中的主版本保存在：

```text
.claude/skills/skill-security-review/
```

Claude Code 从项目 `.claude/skills/` 自动发现该 Skill。批次控制层必须在两套静态报告完成后显式调用 `/skill-security-review`，不能依赖模型自行判断触发时机。审查会话只开放 `Read`、`Glob`、`Grep`，并禁用 MCP 和其他执行类工具。

单个 Skill 的调用信息建议按以下模板提供：

```text
使用 /skill-security-review 审查一个固定内容版本。

skill_root: <只读 Skill 快照目录>
source_metadata: <source-metadata.json>
package_manifest: <package-manifest.json>
cisco_report: <cisco/raw-report.json>
skillspector_report: <skillspector/raw-report.json>
review_id: <本次 AI 审查 ID>
policy_version: <安全规则版本>
reviewed_at: <由批次控制层提供的时间>
model_identifier: <公司内网模型标识>

严格按 review-result.schema.json 返回一个 JSON 对象，不要执行、修改或联网。
```

Claude Code 返回内容由批次控制层保存到受限证据区。AI Skill 本身保持只读，不直接写文件。

受限的非交互调用可按以下形式组织；这是调用模板，不是本阶段要实现的自动化脚本：

```bash
claude -p \
  --tools "Read,Glob,Grep" \
  --disallowedTools "mcp__*" \
  --permission-mode dontAsk \
  --no-session-persistence \
  --output-format json \
  --json-schema '<review-result.schema.json 的压缩内容>' \
  '/skill-security-review <上述调用信息>'
```

`--tools` 用于限制内置工具，`--disallowedTools "mcp__*"` 用于移除 MCP 工具，`dontAsk` 用于拒绝未预先允许的工具请求。使用 `--json-schema` 时，Claude Code 返回的是运行结果外层对象，正式审查结果位于 `structured_output`；批次控制层提取该字段后，还要再次按 Schema 校验，并核对 `source_revision` 与 `skill_digest`。公司内网封装若不支持这些参数，应在等价的会话策略中实现相同限制，不能仅依赖 Skill 正文中的“不执行”要求。

上述发现位置、权限含义和参数格式以 [Claude Code Skills 文档](https://code.claude.com/docs/en/slash-commands) 与 [Claude Code CLI 参考](https://code.claude.com/docs/en/cli-usage) 为准。

### 17.3 并发建议

初次验证可以逐仓库串行处理。全量运行时建议采用有限并发：

- 仓库级并发数量固定并可配置；
- 同一仓库只允许一个仓库准备任务；
- 同仓库多个 Skill 的静态扫描可以在资源允许时并行；
- AI 审查按公司内网模型容量设置并发；
- 所有并发任务使用独立工作区；
- 不因一个仓库失败阻塞其他仓库。

## 18. 运行安全要求

- 下载仓库使用专用只读 SSH 身份；
- 扫描器运行环境不挂载开发者主目录和个人 SSH Key；
- Skill 快照只读；
- 静态检查默认无网络；
- AI 只使用公司内网模型；
- 不向 VirusTotal、公共 OSV、第三方云扫描或公网模型上传内容；
- 扫描进程设置时间、CPU、内存、文件数量和输出大小限制；
- 扫描器本身版本固定并记录校验值；
- 所有关键状态变化写入审计事件；
- 日志不得打印完整密钥、Token 或数据库密码；
- 候选区和受限证据区使用不同访问权限。

## 19. 批次输出

一次批次至少输出以下内容：

### 19.1 批次总览

- 输入仓库数和 Skill 来源行数；
- 实际选中审查的内容版本数；
- 被较新分支替代的来源数；
- 分支内容冲突数；
- `STALE_INVENTORY` 数；
- `PASS`、`REVIEW_REQUIRED`、`BLOCKED`、`INCOMPLETE` 数量；
- 质量等级分布；
- 私密候选数量；
- 工具失败和待重试数量。

### 19.2 Skill 明细

每个 Skill 至少展示：

```text
skill_name
repo_name
source_branch
normalized_skill_path
inventory_revision
source_revision
skill_last_change_revision
skill_digest
Cisco 状态和最高风险
SkillSpector 状态和最高风险
AI 审查状态和最高风险
统一安全结论
质量总分和各维度分
人工确认原因
候选归档状态
证据索引
```

### 19.3 台账回写建议

本方案不直接改写原始 CSV。批次结束后生成独立结果文件供核对，建议增加：

```text
review_batch_id
reviewed_source_revision
reviewed_skill_digest
security_decision
quality_score
review_policy_version
reviewed_at
candidate_status
```

原字段 `security_reviewed` 只能表示“审查流程是否已有完整终态”，不能表示“是否安全通过”。如果继续保留该字段：`PASS` 和已经形成最终结论的 `BLOCKED` 都可以记录为“是”；仍待人工结论的 `REVIEW_REQUIRED` 以及检查不完整的 `INCOMPLETE` 记录为“否”。正式准入必须读取 `security_decision`，不能读取这个布尔字段。

## 20. 验收标准

### 20.1 输入和分支选择

- 能原样读取正式十列 CSV，包括 `latest_commitid` 和三个追溯字段，并兼容旧七列格式；
- 能把 `latest_commitid` 映射为内部台账版本提示；旧 `lasted_commited` 不与新字段混用；
- 所有原始 CSV 行都能通过 `source_row_id` 回查；
- 同一仓库同一路径跨分支时，能按路径最近变化时间选择候选；
- 不使用 SHA 字符串大小判断新旧；
- 较旧分支来源保留为 `SKIPPED_SUPERSEDED_BRANCH`；
- 时间相同但 Digest 不同的版本不会被静默丢弃；
- 台账版本与远端不一致时能识别 `STALE_INVENTORY`。

### 20.2 仓库和快照

- 同一批次同一仓库只下载一次；
- 同仓库不同 revision 的 Skill 能从同一仓库对象分别导出；
- 每个 Skill 快照绑定明确 `source_revision`；
- Digest 覆盖完整 Skill Package；
- 相同内容可以按明确版本条件复用结果；
- 路径越界、LFS 缺失、子模块和特殊文件不会被静默忽略。

### 20.3 审查和评分

- Cisco 与 SkillSpector 能读取同一 Digest 的快照并行执行；
- AI 审查只在两套静态报告形成后执行；
- 三类结果全部保存原始输出和统一结果；
- 重复问题合并展示但保留全部来源；
- 严重和高风险不能被质量得分抵消；
- 工具失败、超时和检查不完整不会被判定为通过；
- 质量得分具有各维度证据和扣分原因；
- 人工确认过程可以追查。

### 20.4 归档和清理

- 受限证据区与私密候选区物理或权限隔离；
- 原始报告不进入候选 Git 工作空间；
- 只有满足安全和质量条件的 Skill 能进入候选区；
- 候选包包含来源 Manifest、Digest 和脱敏摘要；
- 本阶段不会自动 Commit 或 Push；
- 清理前能够验证所有结果已经落盘；
- 失败项保留重试和排查所需信息；
- 处理一个仓库失败不会阻止其他仓库继续。

## 21. 实施前置检查

在后续编写自动化脚本前，应先完成以下准备：

1. 提供一份脱敏 CSV 样例，验证实际状态值、路径格式和编码；
2. 确认 `repo_name` 到 Gerrit SSH 地址的映射规则；
3. 抽样验证 `release/hooks/ref-update` 取得的 Commit 是否为预期更新后版本；
4. 固定 Cisco 和 SkillSpector 的版本、静态模式和报告格式；
5. 确定 AI 审查 Skill 的目录名、输入输出格式和版本号；
6. 确定安全规则版本、质量规则版本和首批质量门槛；
7. 确定受限证据区和私密候选区的实际路径、权限和保留时间；
8. 用少量真实 Skill 验证分支选择、特殊文件、工具失败和结果复用；
9. 完成小批量验收后，再扩大到 100 多个仓库和数百个 Skill。

---

> 本方案的最终落点是：每个候选 Skill 都能证明“来自哪个仓库、哪个分支、哪个冻结 Commit、完整内容是什么、经过哪些检查、发现过什么问题、为什么可以进入私密候选区”，同时不把临时候选仓库误当作正式 SkillHub。
