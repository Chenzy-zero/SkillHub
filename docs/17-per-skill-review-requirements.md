# 逐 Skill 安全审查需求说明

## 1. 目标

对 CSV 中已经筛选为最新版本的 Skill 按仓库分组处理。每个仓库分支只下载一次无历史归档，
提取该组全部台账 Skill 后逐一审查，形成可追溯的 Skill 内容、单项 JSON 结果和批次结果表。

## 2. 输入

必需字段：

```text
skill_id
skill_name
repo_name
branch
skill_path
latest_commitid
security_reviewed
status
```

可选字段：

```text
update_time
history_id
product_line
user_name
user_email
```

CSV 已由上游筛选为需要审查的最新版本，本流程不再比较不同分支的新旧关系。

## 3. 目录要求

```text
<output_root>/
├── git_download/                 当前仓库临时传输和提取目录
├── skills/
│   └── <skill_id>/
│       ├── <skill_name>/         原始 Skill 文件夹，目录内不含 .git
│       ├── source-metadata.json
│       └── review-result.json
└── results/
    ├── skill-review-results.csv
    └── skill-review-results.json
```

`skill_name` 是实际 Skill 文件夹名称。报告和来源元数据位于 `skill_id` 目录下，不在 Skill
文件夹内增加管理文件，避免改变被审查内容。

## 4. 执行要求

1. 按 `repo_name + branch` 分组，组内保留 CSV 顺序；
2. 每个仓库开始前确认临时下载目录没有其他活动仓库；
3. 冻结远程分支 HEAD，一次下载该 revision 的整仓无历史 tar，不下载 `.git` 和提交历史；
4. 只提取 CSV 中登记的 `skill_path`，为组内每个 Skill 形成独立且不含 `.git` 的文件夹；
5. 将各 Skill 迁移到 `skills/<skill_id>/<skill_name>/`，分别计算 Manifest 和 Digest；
6. 对永久 Skill 文件夹逐一执行两套静态检查和 AI 审查；
7. 每完成一个 Skill，立即写入单项 JSON 和批次结果表；
8. 同仓库全部 Skill 完成后清理临时仓库目录，再自动处理下一仓库；
9. 失败记录不得伪装成完成，失败现场按配置决定保留或清理。

## 5. 内容复用要求

- 先按 Skill Root 文件夹名称筛选历史结果；
- 再比较整个 Skill Package 的 SHA-256 Digest；
- 时间戳不参与比较；相对路径、文件内容、Git 权限和符号链接目标参与比较；
- 仅复用安全与质量均通过、版本条件一致、原证据仍有效的结果；
- 每一个 `skill_id` 都必须保留自己的 Skill 文件夹、来源元数据和结果 JSON；
- 复用结果记录原 `skill_id`、原任务、原证据和复用原因；
- 内容一致的 Skill 使用相同的 `content_id`，格式为 `sha256:<skill_digest>`。

## 6. 结果要求

`review-result.json` 是单 Skill 的完整机器可读结论。批次同时输出：

- CSV：保留原清单字段并增加审查字段，供人工查看和 Excel 使用；
- JSON：保留结构化问题和追溯关系，作为后续 HTML 与汇总的正式数据源。

首版不调整 HTML 视觉。后续全局 HTML 应从 JSON 读取数据，并支持按仓库、产品线、
`user_name`、`user_email`、风险和复用状态筛选及导出筛选结果。
