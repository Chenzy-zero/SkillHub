# 工作区与输入规则

## 1. Canonical 路由

- 项目入口：`README.md`、`AGENTS.md`
- 工作流规则：`.agents/rules/`
- Canonical AI policy：`.agents/skills/skill-security-review/`
- `.claude/`、`.codex/`：客户端适配/隔离 Agent，不复制业务策略

根目录不存在的 `docs/`、`rules/`、`skills/` 不得作为核心路由。

## 2. Runtime / State / Archive+Evidence / Reports

profile 默认位于 `.batch-review/<profile>/`；生产配置可使用外部受控目录，但职责不变。
第一阶段保留物理路径兼容，不迁移历史批次。

| 目录 | 类型 | 用途 | 生命周期 |
|---|---|---|---|
| `work` | Runtime | scanner/执行临时区 | 无活动/恢复任务后可受控清理 |
| `git_download` | Runtime | 无历史归档和提取临时区 | 每仓 Static 完成后清理；失败恢复前保留 |
| `manifests` | State | Batch state、task index、AI queue、dispatch lease | 活动批次不可删除；`manifest_root` 只是历史配置键名 |
| `skills` | Archive | 冻结 Skill Package、current/final durable result | 持久审计/复用，不按 Runtime 清理 |
| `restricted-evidence` | Evidence | RAW/SOURCE/DERIVED/NORMALIZED evidence + index | 受限持久证据，不进入普通 AI 上下文 |
| `private-candidates` | Candidate staging | 门禁通过的发布候选 | 不是审计事实，按发布治理处置 |
| `results/<batch-id>` | Reports | HTML、标准化导出、ledger projection | 人工/系统消费输出 |

唯一人工入口：`results/<batch-id>/skill-security-review-report.html`。

`skill-review-results.csv/json` 是 inventory 对账用 ledger projection；`details.csv`、`batch-summary.json`、
`failures.json`、`candidates.json`、`current-review-results.*` 是标准化 report exports。不要混用二者职责。

## 3. CSV 输入

必需字段：

```text
skill_name
repo_name
branch
skill_path
latest_commitid
security_reviewed
status
```

规则：

- `skill_id`、`update_time`、`history_id` 和业务扩展列原样保留，但不能冒充安全结论。
- 兼容 UTF-8、UTF-8 BOM、带 BOM UTF-16、GBK/GB18030；不改写源 CSV。
- 批次保存源 CSV SHA-256、识别编码、原始行号和规范化状态。
- `latest_commitid` 是 inventory revision；实际审查版本由下载阶段重新冻结为 source revision。
- 兼容旧字段 `lasted_commited`，新旧字段同时出现必须报错。
- 输入需要调整时创建新文件，不在运行中的批次里修补原 CSV。

## 4. 文件与秘密

- `SKILL.md` 只定位 Skill Root，审查完整 Root Package。
- 路径逃逸、symlink 越界、特殊文件、归档冲突必须 fail closed。
- 私钥、密码、Token、完整环境变量秘密不得进入日志、报告、AI 上下文或提交。
- 原始 scanner 输出和 imported AI 原始结果只在 restricted evidence 保存；普通报告只含脱敏派生数据和完整性索引。
- RAW/SOURCE evidence 永远 path-only；只有 DERIVED/NORMALIZED 在再次通过 evidence-root/symlink/regular-file 校验后可受控导航。
