# Skill 批量安全审查执行程序

本目录实现 `docs/13-skill-batch-security-review-and-scoring-design.md` 的本地执行部分。
默认启动器现在按仓库一次下载、提取全部台账 Skill 后逐一处理；原仓库级命令仍保留为兼容入口。

首次配置和一键启动见 [`docs/16-skill-batch-review-quick-start.md`](../docs/16-skill-batch-review-quick-start.md)；完整配置字段、逐仓库操作、Claude Code 执行、输出目录和故障处理见 [`docs/15-skill-batch-review-script-user-guide.md`](../docs/15-skill-batch-review-script-user-guide.md)。

```text
CSV 台账
  → 生成仓库/Skill 顺序（不联网）
  → 按 repo_name + branch 冻结一次分支 HEAD
  → 下载一次整仓无历史 tar，只提取该组全部台账 Skill
  → 分别迁移到 skills/<skill_id>/<skill_name>
  → 生成 SHA-256 Digest 和 content_id
  → 检查同名同内容的已通过结果能否复用
  → 同名 Skill Root 内容一致时复用已通过结果
  → Cisco + SkillSpector 并行静态检查
  → 生成 Claude Code 只读交接任务
  → 在公司内网模型环境调用一次 /auto-skill-review
  → 导入并校验 AI JSON
  → 合并问题、安全判定、独立质量评分
  → 在 skill_id 目录写入 review-result.json
  → 原子更新批次结果 CSV/JSON
  → 清理当前仓库临时目录并自动进入下一仓库
```

程序不会执行被审查 Skill 的脚本、安装依赖或调用其中的工具；不会自动调用模型；不会 Commit、Push 或上架候选内容。原始扫描报告保存在受限证据区，不进入私密候选目录。

### 下载能力边界

当前 Gerrit 服务端会忽略 `--filter=blob:none`，不支持 partial clone；不允许对远端未广播的
SHA 直接 fetch；也不接受 `git archive --remote` 的路径限定参数。因此程序按
`repo_name + branch` 用 `git ls-remote` 冻结分支 HEAD，下载一次该 Revision 的整仓无历史归档，
再在本地只提取该仓库清单中的 Skill。整仓归档不会作为完整仓库长期保存，也不会下载 Git 历史。
如果冻结 Revision 无法出包，程序报告 `REPOSITORY_ARCHIVE_UNAVAILABLE`，不会静默切换到其他版本。

## 1. 已实现模块

- CSV 的 UTF-8/UTF-16/GBK 自动识别、原始文件摘要、严格读取、去重、冲突和状态映射；
- 仓库级执行计划，只处理配置中明确列入 `included_statuses` 的状态；
- Gerrit SSH 地址受控生成、单仓库 mirror、处理仓库时冻结分支版本并完成 Commit 对账；
- 按 `repo_name + skill_path` 比较路径最近变更时间，保留跨分支事实；
- 固定 Revision 的只读快照、完整包 SHA-256、特殊文件覆盖记录；
- 同名 Skill Root 快速筛选、忽略时间戳的整包内容比对和已通过结果复用；
- Cisco AI Skill Scanner 与 NVIDIA SkillSpector 的本地静态适配和并行执行；
- Claude Code AI 审查交接、JSON Schema 和冻结版本一致性校验；
- 问题归一化、去重、安全门禁和独立 0–100 质量得分；
- 受限证据、可恢复状态、本地私密候选、批次报告和受控清理；
- 仓库级一次归档、全部台账 Skill 提取、`skills/<skill_id>/<skill_name>` 归档和单项 JSON；
- 同名同内容的 `content_id` 关联、已通过结果复用和批次 CSV/JSON；
- 本地集成测试覆盖完整的两阶段单仓库流程。

## 2. 安装

批处理程序支持 Python 3.11～3.14。Cisco 可使用 Python 3.12～3.14；由于 `yara-python`
官方没有 Python 3.14 wheel，SkillSpector 的 wheel-only 环境使用 Python 3.12 或 3.13。Windows 和 Linux
环境会优先自动发现已安装的兼容 Python；Windows 64 位机器如果只有 3.14，会使用
仓库内经 SHA-256 校验的 Python 3.13.15 官方安装包，安装到 `.scanner-tools/_python313`。
入口会自动优先选择已安装的较新兼容版本：

```bash
cd batch-review
python -m pip install -e '.[dev]'
```

不安装包时可以在本目录使用：

```bash
PYTHONPATH=src python -m skill_batch_review.cli --help
```

### 首次使用的推荐入口

不再要求操作人员手工复制配置、记录批次号或拼接命令参数。

Windows 第一次双击：

```text
batch-review\init.cmd
```

Linux/CentOS 第一次执行：

```bash
./batch-review/init.sh
```

初始化会生成被 Git 忽略的 `batch-review/config/review.local.toml` 和本机操作状态，已有配置
默认绝不覆盖。检查配置后，Windows 始终双击 `batch-review\review.cmd`，Linux/CentOS 始终执行
`./batch-review/review.sh`。该入口会读取真实状态并只提供当前可执行的下一步。

在 Claude Code 中输入 `/ask-cc` 可以只读检查状态；输入 `/auto-skill-review` 可自动完成当前及
后续 AI 审查并推进到下一 Skill、下一仓库。

Windows 测试说明：快照测试中的符号链接用例需要开发者模式或创建符号链接的提升权限。未满足
条件时，测试只会对 `WinError 1314` 自动跳过；其他错误仍会失败。可在“设置 → 隐私和安全性
→ 开发者选项”开启开发者模式。该测试兼容处理不影响实际批量下载和扫描，正式基线验证仍建议
在 Linux 扫描节点执行。

## 3. 配置

复制 `config/review.example.toml`，再填写公司内网的真实值。以下值必须由负责人确认，不能沿用示例占位符：

- `batch.inventory_csv`：正式或脱敏试运行 CSV；
- `batch.included_statuses`：本轮允许进入队列的映射后生命周期状态；
- `gerrit.*`：只读 SSH 地址、账号、端口和仓库白名单；
- `scanners.*.version`：批准并固定的工具版本；
- `workspace.*`：临时工作区、受限证据区、私密候选区和清单区。

AI 策略版本由 `.claude/skills/skill-security-review/` 中的规则内容自动计算 SHA-256；模型由
Claude Code 会话在能可靠识别时记录实际值，否则记录 `claude-code-session`。两者都不要求
操作人员填写。

两套静态命令固定为本地静态模式：

```text
skill-scanner scan <skill_root> --format json --compact --output <output_file>
skillspector scan <skill_root> --no-llm --format json --output <output_file>
```

配置不会接受 Cisco 的 LLM、behavioral、VirusTotal、AI Defense 上传选项，也不会接受 SkillSpector 缺少 `--no-llm` 的命令。

当前正式 CSV 支持：

```text
skill_id,skill_name,repo_name,branch,skill_path,latest_commitid,security_reviewed,status,update_time,history_id
```

旧字段 `lasted_commited` 继续兼容，但不能与 `latest_commitid` 同时出现。版本字段仅作为 `inventory_revision` 提示，实际审查版本在仓库下载后重新冻结为 `source_revision`。

CSV 不要求人工转换为 UTF-8。程序自动识别 UTF-8、UTF-8 BOM、带 BOM 的 UTF-16，以及
Windows Excel 常见的 GBK/GB18030；原文件不会被改写，识别出的编码和原始文件 SHA-256
会写入批次记录。其他编码会明确报错，避免错误猜测导致字段内容被静默改坏。

### 内网 pip 安装静态扫描器

扫描节点不能访问 GitHub且不使用 Docker。公司 pip 源必须同步 `uv==0.12.9`、
`cisco-ai-skill-scanner==2.0.13` 及两套扫描器的全部依赖。SkillSpector 2.5.1 使用本仓库
`packages/` 中的 NVIDIA 官方 wheel，无需公司源另行发布顶层包。随后使用 Python 3.12、3.13 或 3.14 执行：

```bash
python batch-review/tools/install_scanners.py --root /opt/skill-review/scanners
```

脚本先通过当前 pip 源安装固定版 `uv==0.12.9`，再由 uv 为两套工具分别建立和解析隔离环境，
避免 pip 在 Cisco 的大型依赖树上触发 `resolution-too-deep`。Cisco 与 SkillSpector 顶层版本仍
分别固定为 2.0.13 和 2.5.1。默认只安装 wheel；Windows 下仅对 Cisco 依赖链缺少 wheel 的
`win-unicode-console==0.5` 开放单包源码构建例外，其余包仍禁止源码构建。安装完成后，将输出
的两个可执行文件绝对路径填写到 `scanners.*.command[0]`。安装器会核对包元数据，并在代理被阻断、
空 tiktoken 缓存的条件下对内置最小 Skill 执行真实静态扫描；成功后写入
`.scanner-tools/scanner-health.json`。完整步骤见
`docs/15-skill-batch-review-script-user-guide.md`。

Cisco 2.0.13 安装完成后会从其专用环境移除未启用的 `litellm`，避免 CLI 启动时通过 tiktoken
下载 BPE 词表；该环境只允许当前配置中的本地静态规则。SkillSpector 官方 JSON 的
`explanation` 字段会被保留为问题说明，`finding=null` 不再导致 LP3 被误判为报告不完整。

按仓库归档流程带有独立 `workflow_version`。已经执行过的旧批次不会在新流程中继续推进；应保留旧证据，
由 `review.cmd` 创建新批次。完全未开始的旧计划可以安全迁移。

## 4. 标准执行顺序

推荐使用免参数入口：

```text
首次运行 init.cmd / init.sh
→ 后续始终运行 review.cmd / review.sh
→ 等待 AI 时在 Claude Code 输入 /auto-skill-review
→ 自动审查并推进到批次完成或真实阻塞
```

以下带参数命令保留用于自动化运维和故障排查，普通执行人员无需记忆：

日常操作可直接使用跨平台启动器：

```bash
./batch-review/run.sh plan --config batch-review/config/review.company.toml --batch-id baseline-20260901
./batch-review/run.sh start --config batch-review/config/review.company.toml --batch-id baseline-20260901 --execute
./batch-review/run.sh advance --config batch-review/config/review.company.toml --batch-id baseline-20260901 --execute --confirm-cleanup
```

Windows 将 `run.sh` 替换为 `run.cmd`。启动器生成 AI 队列后，在 Claude Code 中调用
`/skill-security-review`；完整操作见快速使用说明。

`run.sh` / `run.cmd` 会维护仓库和 Skill 状态，并把结果写入 `skills_root` 与 `results_root`。

## 5. 仓库级兼容命令

下面的 `skill-batch-review` 命令保留原仓库级流程，供已有测试和迁移使用，不是新批次的
默认入口。以下 `review.toml`、批次号和路径均为示例。

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
2. 在包含本项目的 Claude Code 工作区中调用项目级 Skill：`/skill-security-review`；该入口文件位于 `.claude/skills/skill-security-review/SKILL.md`；
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

兼容命令的清理只允许删除 `workspace.root/<batch_id>/repositories/<当前仓库>/`，并要求该仓库所有 AI 任务已有持久化结果。默认入口只清理
`git_download_root/<batch_id>/<当前仓库任务>/`。受限证据、永久 Skill、结果表和私密候选均不会被清理。

## 6. 输出目录

```text
workspace.root/                 临时镜像、快照和扫描输出，可受控清理
workspace.evidence_root/        原始报告、Manifest、交接和最终结论，限制访问
workspace.candidate_root/       仅安全与质量均符合要求的本地私密候选
workspace.manifest_root/        仓库计划、仓库索引和结果索引
workspace.git_download_root/    当前仓库的无历史 tar 与白名单 Skill 临时目录
workspace.skills_root/          skills/<skill_id>/<skill_name> 与单项 JSON
workspace.results_root/         每个批次的结果 CSV 和 JSON
```

逐 Skill 默认入口固定产生：

```text
skills/<skill_id>/<skill_name>/
skills/<skill_id>/source-metadata.json
skills/<skill_id>/review-result.json
results/<batch_id>/skill-review-results.csv
results/<batch_id>/skill-review-results.json
```

全局 HTML 的数据基础已经由 JSON 提供，视觉和筛选导出界面后续单独调整；原始证据仍只
保存在 `workspace.evidence_root`。仓库级兼容入口继续产生原来的五类报告。

## 7. 当前真实运行前置

代码、本地模拟链路和真实 CSV plan-only 导入已经可运行。正式连接公司环境前仍需提供或确认：Gerrit 只读 SSH 参数、公司内网源中的扫描器依赖、目录权限与保留周期、全批次统一截止时间的版本冻结方式，以及首批小样本仓库。缺少这些输入时不能开始真实批量审查。

## 8. 测试

```bash
PYTHONPATH=src python -W error -m unittest discover -s tests -v
```
