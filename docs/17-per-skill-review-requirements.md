# 逐 Skill 安全审查需求说明

## 1. 目标

对 CSV 中已经筛选为最新版本的 Skill 按行逐个处理。每次只获取、归档和审查一个 Skill，
形成可追溯的 Skill 内容、单项 JSON 结果和批次结果表。

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
├── git_download/                 单 Skill 临时传输目录
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

1. 严格按 CSV 行逐个处理；
2. 每个 Skill 开始前确认临时下载目录为空；
3. 使用 Git partial fetch 获取固定分支的 Commit、目录树及目标 Skill Blob；
4. 服务端不支持 partial clone/filter 时明确失败，不静默下载完整仓库；
5. 从 Git 固定 Commit 导出 `skill_path`，只形成一个不含 `.git` 的 Skill 文件夹；
6. 将 Skill 文件夹迁移到 `skills/<skill_id>/<skill_name>/`；
7. 对永久 Skill 文件夹执行两套静态检查和 AI 审查；
8. 每完成一个 Skill，立即写入单项 JSON 和批次结果表；
9. 结果持久化成功后清理临时下载目录，再处理下一项；
10. 失败记录不得伪装成完成，失败现场按配置决定保留或清理。

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
