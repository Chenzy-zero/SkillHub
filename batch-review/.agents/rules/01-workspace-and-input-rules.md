# 工作区与输入规则

## 1. 目录职责

| 目录 | 用途 | 约束 |
|---|---|---|
| `config/` | 示例和本机配置 | 凭据不提交；本机配置使用 `.local.toml` |
| `inventory/` | Skill 台账 CSV | 原文件只读，批次保存编码和 SHA-256 |
| `tools/`、`src/` | 确定性程序 | 负责输入、下载、扫描、状态、结果和清理 |
| `packages/` | 离线安装材料 | 只使用已批准且经过 SHA-256 核验的文件 |
| `skills/skill-security-review/` | AI 审查策略 | Codex CLI 与 Claude Code 共用的唯一规则和 Schema |
| `.agents/`、`.codex/`、`.claude/` | 客户端适配 | 只负责发现和隔离调度，不复制业务策略 |
| `.batch-review/` | 本机运行区 | 状态、Skill 副本、受限证据和批次结果，不提交 Git |
| `tests/` | 自动化测试 | 不执行被审查 Skill，不使用真实生产凭据 |

生产环境因容量或权限需要使用外部工作区时，必须在配置中明确指定专用受控路径。该路径只能
用于本批次审查，不得把父目录或相邻项目隐式加入输入范围。

## 2. CSV 输入

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
- 只校验必需字段；新增产品线、部门、用户等扩展列不得导致导入失败。
- 兼容 UTF-8、UTF-8 BOM、带 BOM 的 UTF-16 和 GBK/GB18030，不改写原文件。
- 批次必须保存原文件 SHA-256、识别编码、原始行号和规范化结果。
- `latest_commitid` 作为台账版本输入，最终审查版本必须从 Git 冻结并完成对账。
- 兼容旧字段 `lasted_commited`，但新旧字段同时出现时必须报错。
- 输入需要调整时生成新文件，不在批次执行中就地修复原 CSV。

## 3. 文件与秘密

- Skill 副本只保存 `<skill_id>/<skill_name>/`，不得包含 `.git`。
- 路径逃逸、符号链接越界、归档条目异常或目标目录冲突时停止处理。
- 私钥、密码、Token、环境变量值和完整秘密不得进入输出、AI 上下文或提交。
- 原始扫描输出和 AI 原始结果存放在受限证据区；普通报告只包含脱敏派生数据和证据索引。
