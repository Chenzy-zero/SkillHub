# 逐 Skill 下载、归档、复用与结果设计

## 1. 总体流程

```text
读取并冻结 CSV
  ↓
按输入顺序取得一条 Skill
  ↓
校验 skill_id、名称、仓库、分支、路径和 Commit
  ↓
清理并建立本项 git_download 临时目录
  ↓
Blobless Partial Fetch 固定分支
  ↓
确认 FETCH_HEAD == latest_commitid
  ↓
只导出 skill_path
  ↓
计算 Package Manifest 与 SHA-256 Digest
  ↓
原子迁移到 skills/<skill_id>/<skill_name>/
  ↓
同名 + 同 Digest + 审查版本一致？
  ├── 是：生成当前 Skill 的 RESULT_REUSED 结果
  └── 否：Cisco 与 SkillSpector → AI → 综合判定
  ↓
写 source-metadata.json 与 review-result.json
  ↓
原子重建批次 CSV/JSON
  ↓
清理当前 git_download
  ↓
下一条 Skill
```

## 2. 下载边界

临时目录结构：

```text
git_download/<batch_id>/<task_id>/
├── .transport.git/       仅用于 partial fetch 的临时裸仓库
└── <skill_name>/         从固定 Commit 导出的纯 Skill 文件夹
```

传输命令使用参数数组，不经过 Shell：

```text
git init --bare .transport.git
git remote add origin <受控 Gerrit URL>
git fetch --no-tags --depth=1 --filter=blob:none origin refs/heads/<branch>
```

若 Git 输出表明服务端忽略 `filter`，任务以 `PARTIAL_CLONE_UNSUPPORTED` 停止。导出阶段使用
`git ls-tree` 和 `git cat-file` 按需读取目标 Skill Blob，不 Checkout、不运行 Hook、不运行
仓库内容。

## 3. 永久 Skill 目录

```text
skills/<skill_id>/
├── <skill_name>/
│   ├── SKILL.md
│   └── ...
├── source-metadata.json
└── review-result.json
```

路径组件必须拒绝 `/`、反斜线、`.`、`..`、控制字符和符号链接。已存在目录只有在来源与
Digest 一致时允许幂等继续；不同内容不得覆盖，进入 `OUTPUT_CONFLICT`。

## 4. 身份模型

- `skill_id`：上游台账给出的当前记录目录标识；
- `source_id`：仓库、分支、路径、名称和 Commit 的稳定摘要；
- `skill_digest`：完整 Skill Package 内容摘要；
- `content_id`：`sha256:<skill_digest>`，相同内容必然相同；
- `review_id`：当前审查任务标识；
- `reused_from_skill_id`：复用来源的台账标识。

`skill_id` 相同但来源或内容不同时禁止覆盖。相同内容但不同 `skill_id` 各自保留一份目录，
通过同一个 `content_id` 建立关联。

## 5. 结果复用

索引键：

```text
skill_root_name
+ skill_digest
+ Cisco 版本和配置摘要
+ SkillSpector 版本和配置摘要
+ 策略版本
+ AI 审查 Skill 摘要
+ 模型标识
+ JSON Schema 摘要
+ 质量门槛
```

复用不复制旧来源字段。系统以当前 Skill 的来源生成新 `review-result.json`，仅复用经过验证
的问题、结论和质量分，并记录完整来源关系。原证据缺失、结论不是 `PASS`、版本变化或
内容不同，均执行完整审查。

## 6. 单项 JSON

```text
schema_version
skill_id
skill_name
repo_name
branch
skill_path
inventory_revision
source_revision
skill_digest
content_id
product_line
user_name
user_email
review_status
security_decision
quality_decision
quality_score
max_severity
finding_counts
findings
reuse_status
reused_from_skill_id
reused_from_task_id
review_policy_version
reviewed_at
evidence_ref
failure_reason
```

## 7. 批次结果

JSON 使用一个批次对象和 `skills` 数组，保留每个单项 JSON 的结构。CSV 保留输入列顺序，
并追加摘要字段：

```text
review_status,security_decision,quality_decision,quality_score,max_severity,
finding_count,critical_count,high_count,medium_count,low_count,info_count,
content_id,reuse_status,reused_from_skill_id,review_result_path,evidence_ref,
review_policy_version,reviewed_at,failure_reason
```

每完成一项，以临时文件、`fsync` 和原子替换重建 CSV/JSON，保证中断后已完成结果可恢复。
