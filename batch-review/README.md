# Skill 批量安全审查执行程序

本目录实现 `docs/13-skill-batch-security-review-and-scoring-design.md` 的本地执行部分。程序按仓库逐个处理，明确分成两个阶段：

完整的配置字段、逐仓库操作、Claude Code 执行、输出目录和故障处理见 [`docs/15-skill-batch-review-script-user-guide.md`](../docs/15-skill-batch-review-script-user-guide.md)。

```text
CSV 台账
  → 生成仓库顺序（不联网）
  → 单仓库 SSH 镜像
  → 冻结来源版本并选择跨分支最新内容
  → 导出只读 Skill 快照和 SHA-256 Digest
  → Cisco + SkillSpector 并行静态检查
  → 生成 Claude Code 只读交接任务
  → 人工在公司内网模型环境运行 /skill-security-review
  → 导入并校验 AI JSON
  → 合并问题、安全判定、独立质量评分
  → 符合条件的内容导出到本地私密候选区
  → 生成报告
  → 明确确认后清理该仓库临时工作区
```

程序不会执行被审查 Skill 的脚本、安装依赖或调用其中的工具；不会自动调用模型；不会 Commit、Push 或上架候选内容。原始扫描报告保存在受限证据区，不进入私密候选目录。

## 1. 已实现模块

- 七列 CSV 的严格读取、原始文件摘要、去重、冲突和状态映射；
- 仓库级执行计划，只处理配置中明确列入 `included_statuses` 的状态；
- Gerrit SSH 地址受控生成、单仓库 mirror、处理仓库时冻结分支版本并完成 Commit 对账；
- 按 `repo_name + skill_path` 比较路径最近变更时间，保留跨分支事实；
- 固定 Revision 的只读快照、完整包 SHA-256、特殊文件覆盖记录；
- Cisco AI Skill Scanner 与 NVIDIA SkillSpector 的本地静态适配和并行执行；
- Claude Code AI 审查交接、JSON Schema 和冻结版本一致性校验；
- 问题归一化、去重、安全门禁和独立 0–100 质量得分；
- 受限证据、可恢复状态、本地私密候选、批次报告和受控清理；
- 本地集成测试覆盖完整的两阶段单仓库流程。

## 2. 安装

要求 Python 3.11 或更高版本：

```bash
cd batch-review
python -m pip install -e '.[dev]'
```

不安装包时可以在本目录使用：

```bash
PYTHONPATH=src python -m skill_batch_review.cli --help
```

## 3. 配置

复制 `config/review.example.toml`，再填写公司内网的真实值。以下值必须由负责人确认，不能沿用示例占位符：

- `batch.inventory_csv`：正式或脱敏试运行 CSV；
- `batch.included_statuses`：本轮允许进入队列的映射后生命周期状态；
- `gerrit.*`：只读 SSH 地址、账号、端口和仓库白名单；
- `scanners.*.version`：批准并固定的工具版本；
- `ai.policy_version`、`ai.reviewer_model`：审查规则和公司内网模型标识；
- `workspace.*`：临时工作区、受限证据区、私密候选区和清单区。

两套静态命令固定为本地静态模式：

```text
skill-scanner scan <skill_root> --format json --compact --output <output_file>
skillspector scan <skill_root> --no-llm --format json --output <output_file>
```

配置不会接受 Cisco 的 LLM、behavioral、VirusTotal、AI Defense 上传选项，也不会接受 SkillSpector 缺少 `--no-llm` 的命令。

CSV 必须包含且只能包含：

```text
skill_name,repo_name,branch,skill_path,lasted_commited,security_reviewed,status
```

`lasted_commited` 仅作为 `inventory_revision` 提示。实际审查版本在仓库下载后重新冻结为 `source_revision`。

## 4. 标准执行顺序

下面的 `review.toml`、批次号和路径均为示例。

### 步骤 1：本地校验和仓库计划

```bash
skill-batch-review validate-config review.toml
skill-batch-review preflight review.toml
skill-batch-review init-batch review.toml --batch-id baseline-20260831
skill-batch-review plan-repositories review.toml --output repository-plan.json
```

这四步不访问 Gerrit、不执行扫描器。`repository-plan.json` 中的 `repositories_to_prepare` 是后续处理顺序。

### 步骤 2：准备一个仓库

```bash
skill-batch-review prepare-repository review.toml \
  --batch-id baseline-20260831 \
  --repository team/repo
```

这是明确的网络和扫描边界。命令只处理一个仓库，输出 `repository_index` 和 `ai_review_queue`。相同仓库内的多个 Skill 共用一份 mirror，但各自使用冻结 Revision 的独立快照。

### 步骤 3：运行 Claude Code AI 审查

对 `ai_review_queue` 中每项依次执行：

1. 在公司内网模型环境启动 Claude Code；
2. 调用项目 Skill：`/skill-security-review`；
3. 把该任务 `handoff` JSON 作为唯一任务上下文；
4. 只开放读取能力，不开放 Bash、写文件、网络、MCP 或子代理；
5. 将最终纯 JSON 保存为 `<ai-results-dir>/<task_id>.json`。

具体 Claude Code 启动参数由公司已批准版本决定，本程序不猜测或自动调用模型。

### 步骤 4：导入结果并生成候选

```bash
skill-batch-review finalize-repository review.toml \
  --batch-id baseline-20260831 \
  --repository-index /path/to/repository-index.json \
  --ai-results-dir /path/to/ai-results
```

只有满足以下全部条件的 Skill 才会进入本地私密候选区：

- 快照和两套静态报告完整且 Digest 一致；
- AI JSON 通过 Schema 和冻结版本校验；
- 安全结论为 `PASS`；
- 质量结论为 `PASS` 且当前得分不少于 70；
- 没有需要人工确认的分支冲突、特殊文件或其他问题。

### 步骤 5：汇总与清理

```bash
skill-batch-review report-batch review.toml \
  --batch-id baseline-20260831 \
  --results-dir /path/to/repository-result-indexes \
  --output-dir /path/to/batch-reports

skill-batch-review cleanup-repository review.toml \
  --batch-id baseline-20260831 \
  --repository team/repo \
  --repository-index /path/to/repository-index.json \
  --confirm-cleanup
```

清理只允许删除 `workspace.root/<batch_id>/repositories/<当前仓库>/`，并要求该仓库所有 AI 任务已有持久化结果。受限证据、manifest 和私密候选位于工作区之外，不会被清理。完成一个仓库后再进入计划中的下一个仓库。

## 5. 输出目录

```text
workspace.root/                 临时镜像、快照和扫描输出，可受控清理
workspace.evidence_root/        原始报告、Manifest、交接和最终结论，限制访问
workspace.candidate_root/       仅安全与质量均符合要求的本地私密候选
workspace.manifest_root/        仓库计划、仓库索引和结果索引
```

批次报告固定产生：

```text
batch-summary.json
details.csv
failures.json
candidates.json
```

## 6. 当前真实运行前置

代码和本地模拟链路已经可运行。正式连接公司环境前仍需提供或确认：真实 CSV 的脱敏样例与完整 `status` 枚举、Gerrit 只读 SSH 参数、两套工具固定版本和安装路径、公司内网模型标识、目录权限与保留周期、全批次统一截止时间的版本冻结方式，以及首批小样本仓库。缺少这些输入时只能执行本地校验和模拟测试，不能开始真实批量审查。

## 7. 测试

```bash
PYTHONPATH=src python -W error -m unittest discover -s tests -v
```
