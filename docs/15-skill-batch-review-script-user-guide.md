# Skill 批量安全审查脚本详细使用说明

> 文档版本：V1.0
>
> 适用程序：`batch-review/`
>
> 适用范围：公司 Gerrit 台账中的存量 Skill
>
> 配套设计：[`13-skill-batch-security-review-and-scoring-design.md`](13-skill-batch-security-review-and-scoring-design.md)
>
> 实施任务：[`14-skill-batch-review-implementation-tasks.md`](14-skill-batch-review-implementation-tasks.md)
>
> 快速入口：[`16-skill-batch-review-quick-start.md`](16-skill-batch-review-quick-start.md)
>
> 逐 Skill 设计：[`18-per-skill-review-design.md`](18-per-skill-review-design.md)

## 当前默认启动方式

`batch-review/run.sh` 和 `run.cmd` 已切换为逐 Skill 启动器。CSV 中每个纳入行必须有唯一
`skill_id`，并由上游保证是需要审查的最终版本。默认流程为：

```text
逐 Skill partial fetch
→ 只导出 skill_path
→ skills/<skill_id>/<skill_name>/
→ 静态与 AI 审查或结果复用
→ skills/<skill_id>/review-result.json
→ results/<batch_id>/skill-review-results.csv/json
→ 清理当前 git_download
```

新增配置：

```toml
[workspace]
git_download_root = "/data/skill-review/git_download"
skills_root = "/data/skill-review/skills"
results_root = "/data/skill-review/results"
```

Gerrit 必须支持 `--filter=blob:none`。服务端忽略过滤时程序返回
`PARTIAL_CLONE_UNSUPPORTED`，不会静默退回完整仓库下载。可选清单字段统一使用
`product_line`、`user_name`、`user_email`。

面向普通操作人员的默认入口进一步简化为：首次使用 `init.cmd`/`init.sh`，之后始终使用
`review.cmd`/`review.sh`。免参数入口自动保存本机配置路径和当前批次号，并根据状态调用底层
逐 Skill 启动器。原 `run.sh`/`run.cmd` 继续作为带参数的运维入口。

## 1. 文档目的

本文面向实际执行批量审查的 CM、安全人员和平台维护人员，说明如何使用 `batch-review/` 完成以下工作：

```text
读取 CSV 台账
→ 确定需要处理的仓库
→ 每次下载一个仓库
→ 冻结并导出该仓库中的 Skill
→ 并行运行两套静态检查
→ 生成 Claude Code AI 审查任务
→ 导入 AI 结果
→ 形成安全结论和质量得分
→ 导出本地私密候选
→ 生成批次报告
→ 清理单仓库临时工作区
```

本文只说明当前已经实现的命令和操作步骤，不把尚未实现的自动调度能力描述成可用功能。

## 2. 重要边界

脚本会执行以下动作：

- 读取并校验指定 CSV；
- 通过配置的 Gerrit SSH 地址下载或更新单个仓库 mirror；
- 从固定 Git Revision 导出 Skill 目录；
- 执行 Cisco AI Skill Scanner 和 NVIDIA SkillSpector；
- 生成 Claude Code 审查交接 JSON；
- 校验人工保存的 AI 审查 JSON；
- 在本地生成证据、结果、报告和私密候选目录；
- 在明确确认后删除当前仓库的临时工作区。

脚本不会执行以下动作：

- 不执行被审查 Skill 中的脚本、安装程序、命令或 MCP 工具；
- 不安装 Skill 声明的依赖；
- 不自动调用 Claude Code 或公司内网模型；
- 不把 Skill 或报告上传到公网服务；
- 不修改原始 CSV；
- 不修改 Gerrit 中的原仓库；
- 不自动 Commit、Push 私密候选；
- 不自动同步或上架 SkillHub；
- 不自动替代产品线确认 Skill 是否上架。

## 3. 当前版本的执行限制

### 3.1 默认按 Skill 逐个执行

默认启动器一次只准备一个 CSV Skill。由 `start` 和 `advance` 按输入顺序重复以下流程：

```text
准备一个 Skill
→ 完成该 Skill 的 AI 审查或确认复用
→ 导入结果
→ 写入单项和批次结果
→ 清理该 Skill 的 git_download
→ 处理下一个 Skill
```

`batch-review/run.sh` 与 `run.cmd` 已负责记住当前进度、完成当前 Skill 并准备下一个 Skill。
AI 审查仍是明确的人工检查点，因此不会用一条无人值守命令自动跑完整个批次。
原来的 `skill-batch-review prepare-repository/finalize-repository` 保留为兼容入口。

### 3.2 Claude Code 必须人工执行

`prepare-repository` 只生成 AI 任务，不会启动 Claude Code。执行人员必须在公司内网模型环境中调用项目级 `/skill-security-review`，再把纯 JSON 结果保存到约定目录。

### 3.3 全批次统一截止时间尚未自动冻结

当前实现会在处理某个仓库时冻结该仓库的分支版本。若 100 多个仓库需要严格使用同一个批次截止时间，正式全量运行前还需要补充远端 Revision 冻结清单。

首轮小批量联调可以按仓库处理时冻结；全量正式批次不应忽略这一差异。

### 3.4 重试和并发配置尚未成为全自动调度

配置中的 `[retry]` 和 `[concurrency]` 用于固定批次运行策略。启动器会按计划逐仓库推进，
但不会绕过每仓库 AI 检查点连续跑完整批次，也不会按照这些值自动完成跨仓库重试。

当前已实现的并行行为只有：同一个 Skill 的 Cisco 与 SkillSpector 静态检查并行执行。

### 3.5 清理必须显式执行

使用底层 `finalize-repository` 时，仍需单独运行
`cleanup-repository --confirm-cleanup`。使用启动器时，执行
`advance --execute --confirm-cleanup` 会在结果持久化后清理当前仓库，再进入下一仓库；
缺少确认参数时不会清理或推进。

## 4. 目录结构

```text
batch-review/
├── config/
│   ├── review.example.toml          通用脱敏配置样例
│   └── review.company.example.toml  公司环境待填写模板
├── examples/
│   └── inventory.example.csv        七列 CSV 样例
├── src/skill_batch_review/
│   ├── cli.py                       命令行入口
│   ├── config.py                    TOML 配置校验
│   ├── inventory.py                 CSV 读取和规范化
│   ├── git_source.py                仓库、分支和来源版本对账
│   ├── snapshot.py                  Skill 快照和 SHA-256
│   ├── scanners.py                  两套静态检查适配
│   ├── ai_review.py                 AI 交接和结果校验
│   ├── review_policy.py             安全判定和质量门槛
│   ├── artifacts.py                 证据和候选导出
│   ├── reporting.py                 批次报告
│   └── orchestrator.py              单仓库两阶段流程
├── tests/                            本地测试
├── tools/run_batch.py                逐仓库启动与续跑器
├── run.sh                            Linux/CentOS 入口
├── run.cmd                           Windows 入口
├── README.md                         快速使用说明
└── pyproject.toml                    安装和依赖定义
```

AI 审查 Skill 位于 Claude Code 项目级自动发现目录：

```text
.claude/skills/skill-security-review/
├── SKILL.md
└── references/
    ├── security-review.md
    ├── quality-review.md
    ├── upstream-vetter-checklist.md
    ├── upstream-source.json
    └── review-result.schema.json
```

该目录中的 `skill-security-review` 是公司维护的审查入口，参考
UseAI-pro 的 `skill-vetter` 与 `skill-auditor`，但不是任何上游 Skill 的原样副本。

## 5. 运行环境要求

### 5.1 基础环境

- 正式扫描节点推荐 Linux；当前确认使用 CentOS 7.9；
- 批处理程序支持 Python 3.11～3.14，扫描器安装支持 Python 3.12～3.14；
- Git 命令行；
- 能访问公司 Gerrit 的网络环境；
- Gerrit 专用只读 SSH 身份；
- Cisco AI Skill Scanner；
- NVIDIA SkillSpector；
- 公司批准版本的 Claude Code；
- 公司内网模型。

Windows 可以作为配置、发起和查看结果的操作端，但首批正式扫描不建议直接在 Windows 执行。原因是当前尚未在 Windows 验证 Git 符号链接、文件权限位、路径大小写和快照摘要是否与 Linux 一致。Windows 应通过 SSH 连接 CentOS 扫描节点执行以下命令。

CentOS 7.9 已停止主流维护，扫描节点应隔离部署、使用只读 Gerrit 账号并限制出站网络。不要使用系统自带 Python；单独安装 Python 3.12、3.13 或 3.14。使用 3.14 时必须确认公司内网源具备兼容的二进制 wheel。

### 5.2 批处理程序安装

在仓库根目录执行：

```bash
cd batch-review
python3 -m pip install -e '.[dev]'
```

安装成功后应存在：

```bash
skill-batch-review --help
```

如果不希望安装命令行入口，可以在 `batch-review/` 下使用：

```bash
PYTHONPATH=src python3 -m skill_batch_review.cli --help
```

本文后续统一使用 `skill-batch-review`。两种运行方式功能相同。

### 5.3 从公司内网 pip 源安装扫描器

扫描器使用两个隔离虚拟环境，避免 Cisco 与 SkillSpector 的依赖互相覆盖。安装脚本不会访问 GitHub，也不会安装被审查 Skill 的依赖。

公司内网源必须先具备：

```text
uv==0.12.9
cisco-ai-skill-scanner==2.0.13
skillspector==2.5.1
以及两者在目标 Python 3.12～3.14 下的全部二进制依赖包
```

Cisco 已公开发布 PyPI 包，可以由公司代理同步。SkillSpector 2.5.1 当前没有公开 PyPI 发布包，制品管理员必须在允许访问上游源码的受控构建环境中，基于批准的源码提交构建 wheel、记录 SHA-256 和许可证信息，再上传公司内网源。扫描节点不得从 GitHub 安装，也不得使用未固定的 `main` 分支。

安装器不会直接让 pip 解析 Cisco 的完整依赖树。它只用 pip 从同一包源安装固定版本
`uv==0.12.9`，随后通过 uv 安装固定版本 Cisco 和 SkillSpector。这样可避免 pip 在
`onnxruntime`、`litellm`、`jmespath` 等间接依赖上长时间回溯并报
`resolution-too-deep`。如果只在命令中重复指定 `cisco-ai-skill-scanner==2.0.13`，不能解决
该问题，因为顶层版本原本就已经固定。

在仓库根目录执行：

```bash
python3.12 batch-review/tools/install_scanners.py \
  --root /opt/skill-review/scanners
```

Windows 试运行节点可以使用 Python 3.12～3.14，例如：

```powershell
py -3.14 batch-review/tools/install_scanners.py `
  --root C:\skill-review\scanners
```

Windows 输出的程序通常位于 `cisco\Scripts\skill-scanner.exe` 和 `skillspector\Scripts\skillspector.exe`。这只用于兼容性试跑；首批正式结果仍以 CentOS/Linux 节点为准。

如果公司 pip 地址没有配置在 `pip.conf`，可以临时指定不含明文密码的地址：

```bash
python3.12 batch-review/tools/install_scanners.py \
  --root /opt/skill-review/scanners \
  --index-url https://pypi.company.example/simple \
  --json
```

不要把带用户名、密码或 Token 的 URL 写入仓库、命令历史或日志。优先由运维在扫描账号的 `pip.conf` 中配置公司认证方式。

脚本会从显式 `--index-url`、`PIP_INDEX_URL` 或现有 `pip.ini`/`pip.conf` 读取同一个包源，
再安全地传给 uv；不会访问 GitHub。默认强制只安装 wheel。Windows 下 Cisco 的依赖链
`oletools → pcodedmp → win-unicode-console` 存在一个上游未发布 wheel 的例外，因此安装器仅
允许固定的 `win-unicode-console==0.5` 从源码包在隔离构建环境中生成 wheel。该例外不扩展到
其他包；Linux/CentOS 仍保持全部 wheel。公司包源需要同步这个固定版本的源码制品并完成内部
审核。如果其他依赖缺少 wheel，安装仍会停止。

安装器完成后使用 Python 包元数据核对实际版本，不会启动
`skill-scanner.exe --version` 或 `skillspector.exe --version`。这是为了避免某些上游命令行入口在解析
`--version` 之前就加载可选组件，从而意外发起网络请求。版本是否符合固定值仍会严格校验。

遇到截图中的错误后无需删除 `.scanner-tools`，拉取本次修复并再次双击 `review.cmd` 即可；
安装过程是可重复执行的。

安装完成后，把脚本输出的绝对路径写入 `review.toml`。Linux 示例：

```toml
[scanners.cisco]
enabled = true
version = "2.0.13"
timeout_seconds = 600
command = [
  "/opt/skill-review/scanners/cisco/bin/skill-scanner", "scan", "{skill_root}",
  "--format", "json", "--compact", "--output", "{output_file}",
]

[scanners.skillspector]
enabled = true
version = "2.5.1"
timeout_seconds = 600
command = [
  "/opt/skill-review/scanners/skillspector/bin/skillspector", "scan", "{skill_root}",
  "--no-llm", "--format", "json", "--output", "{output_file}",
]
```

### 5.4 扫描器检查

确认命令可以找到：

```bash
command -v skill-scanner
command -v skillspector
```

当前程序只接受以下本地静态参数：

```text
skill-scanner scan <skill_root> --format json --compact --output <output_file>
skillspector scan <skill_root> --no-llm --format json --output <output_file>
```

Cisco 配置不得增加 LLM、behavioral、VirusTotal 或 AI Defense 上传选项；SkillSpector 必须保留 `--no-llm`。

不要手工执行扫描器的 `--version` 作为安装验收；重新执行上述安装器即可通过包元数据
完成无网络版本校验。SkillSpector 即使使用 `--no-llm` 仍会尝试把依赖名称和版本发送给 OSV.dev；公司
环境不允许该出站访问时，应在网络层阻断，它会退回内置离线规则。该限制必须记录到扫描覆盖信息中。

### 5.5 Gerrit SSH 准备

运行前应确认：

- 只读账号可以读取目标仓库和分支；
- SSH 私钥不放在仓库、配置文件、工作区或日志中；
- Gerrit 主机指纹已通过公司规定方式加入 `known_hosts`；
- SSH 地址模板与实际 Gerrit 项目路径一致；
- 账号没有向远端仓库 Push 的权限，或执行环境明确禁止写操作。

可以先使用公司批准的只读方式验证单个仓库。不要把密码、Token 或私钥内容写进测试命令和聊天记录。

## 6. CSV 输入说明

### 6.1 固定表头

当前正式 CSV 使用以下字段：

```text
skill_id,skill_name,repo_name,branch,skill_path,latest_commitid,security_reviewed,status,update_time,history_id
```

示例：

```csv
skill_id,skill_name,repo_name,branch,skill_path,latest_commitid,security_reviewed,status,update_time,history_id
id-001,jira-query,team/Skill-CM,refs/heads/main,tools/jira-query,1111111111111111111111111111111111111111,否,新增,2026/8/31 10:00:00,1001
```

### 6.2 字段含义

| 字段 | 用途 | 处理规则 |
|---|---|---|
| `skill_name` | 台账中的 Skill 名称 | 用于展示和来源身份校验，不用于自动合并不同路径 |
| `repo_name` | Gerrit 项目路径 | 必须是规范化相对项目名，不接受 URL、绝对路径或 `..` |
| `branch` | 来源分支 | `refs/heads/main` 会规范化为 `main` |
| `skill_path` | `SKILL.md` 所在目录 | 使用 POSIX 相对路径；仓库根目录写 `/` 或 `.` |
| `latest_commitid` | 当前台账 Commit 字段 | 导入后命名为 `inventory_revision`；必须是完整 40 或 64 位十六进制值 |
| `security_reviewed` | 旧台账审查提示 | 只保留，不作为首轮全量审查的跳过条件 |
| `status` | 台账生命周期状态 | 必须在配置的 `status_mapping` 中出现 |
| `skill_id` | 台账 Skill 标识 | 作为追溯字段保留，不替代 Source 身份和内容校验值 |
| `update_time` | 台账更新时间 | 作为追溯字段保留，不直接决定 Git 中的最新内容 |
| `history_id` | 台账历史记录标识 | 作为追溯字段保留 |

旧清单的 `lasted_commited` 仍兼容，但不能与 `latest_commitid` 同时出现。两列同时出现会因版本来源不明确而拒绝导入。

### 6.3 CSV 编码兼容

执行人员不需要提前把 CSV 手工转换为 UTF-8。程序按以下顺序自动识别：

1. UTF-8 和 UTF-8 BOM；
2. 带 BOM 的 UTF-16 LE/BE（Windows Excel 常见）；
3. GB18030（兼容 GBK 和 GB2312）。

程序只在内存中解码，不修改原始 CSV。批次清单和结果 JSON 同时保存
`inventory_csv_encoding` 与原文件 SHA-256，确保来源可追溯。没有 BOM 的 UTF-16 或其他
未知编码会被拒绝，不使用概率猜测，避免中文或路径被静默解码成错误内容。

### 6.4 重要规则

- `latest_commitid`（或兼容字段 `lasted_commited`）不是最终审查版本；
- 脚本下载仓库后会重新冻结分支版本并与 CSV 对账；
- `security_reviewed=是` 不会让该行跳过首轮全量审查；
- 完全重复的 CSV 行只执行一次，但保留全部原始行号；
- 同一来源出现不同 Commit 或不同状态时会标记输入冲突；
- 未知 `status` 会直接阻止导入，不会默认当作有效；
- 原始 CSV 会计算 SHA-256 并记录识别编码，脚本不会改写它。

## 7. 配置文件说明

先复制样例：

```bash
cp batch-review/config/review.example.toml /secure/path/review.toml
```

不要直接使用样例中的地址、版本和模型占位值运行真实批次。

所有相对路径均相对于 `review.toml` 所在目录解析，不是相对于当前终端目录解析。

### 7.1 `[batch]`

```toml
[batch]
inventory_csv = "/secure/input/skills.csv"
batch_id_prefix = "skill-review"
included_statuses = ["ACTIVE"]
```

| 配置 | 说明 |
|---|---|
| `inventory_csv` | 默认 CSV 路径；命令行 `--csv` 可以临时覆盖 |
| `batch_id_prefix` | 未指定 `--batch-id` 时的批次号前缀 |
| `included_statuses` | 映射后允许进入审查队列的状态，必须显式填写 |

`included_statuses` 必须是 `status_mapping` 右侧真实产生的状态。不要为了让配置通过而随意填写。

### 7.2 `[workspace]`

```toml
[workspace]
root = "/data/skill-review/work"
evidence_root = "/data/skill-review/restricted-evidence"
candidate_root = "/data/skill-review/private-candidates"
manifest_root = "/data/skill-review/manifests"
clean_after_repository = true
keep_failed_workspace = false
```

| 配置 | 内容 | 访问建议 |
|---|---|---|
| `root` | 临时镜像、Skill 快照和扫描器临时输出 | 执行账号可读写；可在结果落盘后清理 |
| `evidence_root` | 原始扫描报告、AI 交接、AI 原始结果和完整最终结果 | 仅审查人员和安全人员访问 |
| `candidate_root` | 通过门槛的本地私密候选 | 仅指定管理人员访问，不自动 Git Push |
| `manifest_root` | 仓库计划、仓库索引、结果索引 | 可供批次管理和报告程序读取 |

四个目录必须相互分离。`evidence_root`、`candidate_root`、`manifest_root` 不能位于 `root` 内，否则配置校验会拒绝，防止清理临时工作区时误删证据。

`clean_after_repository` 和 `keep_failed_workspace` 当前作为运行策略记录；实际删除仍只由显式的 `cleanup-repository` 命令执行。

### 7.3 `[gerrit]`

```toml
[gerrit]
ssh_url_template = "ssh://{user}@{host}:{port}/{repo_name}.git"
user = "readonly-skill-review"
host = "gerrit.company.example"
port = 29418
allowed_repositories = ["team/repo-a", "team/repo-b"]
```

支持的占位符：

```text
{repo_name}
{branch}
{user}
{host}
{port}
```

建议首轮小批量使用 `allowed_repositories` 白名单。空数组表示允许 CSV 中所有格式合法的仓库，不表示拥有读取权限。

配置中不要放密码、Token、私钥路径展开内容或带凭据的 HTTPS URL。

### 7.4 `[status_mapping]`

```toml
[status_mapping]
"新增" = "ACTIVE"
"修改" = "ACTIVE"
"删除" = "DELETED"
```

左侧必须覆盖真实 CSV 中可能出现的全部值。右侧是程序内部使用的状态。

示例中只有 `ACTIVE` 被 `[batch].included_statuses` 纳入，因此 `INACTIVE` 和 `DELETED` 会保留在仓库计划的排除清单中，但不会下载和检查。

### 7.5 `[quality]`

```toml
[quality]
candidate_threshold = 70
max_score = 100
```

质量得分与安全结论分开：

- 安全不是 `PASS` 时，质量分再高也不能进入候选区；
- 质量低于 `candidate_threshold` 时，即使安全通过也不进入候选区；
- 当前设计的私密候选门槛是 70 分。

### 7.6 `[ai]`

```toml
[ai]
skill_path = "/path/to/SkillHub/.claude/skills/skill-security-review"
result_schema_path = "/path/to/SkillHub/.claude/skills/skill-security-review/references/review-result.schema.json"
```

| 配置 | 说明 |
|---|---|
| `skill_path` | Claude Code 使用的项目 AI 审查 Skill |
| `result_schema_path` | AI 最终 JSON 的严格 Schema |
`policy_version` 由上述审查 Skill 的 `SKILL.md` 和 `references/` 内容自动计算，文件时间戳和
`evals/` 不参与。Claude Code 能可靠取得实际模型时写入实际标识，否则使用
`claude-code-session` 作为运行入口追溯。操作人员不填写这两个字段。

### 7.7 `[scanners.*]`

```toml
[scanners.cisco]
enabled = true
version = "批准的实际版本"
timeout_seconds = 600
command = [
  "skill-scanner", "scan", "{skill_root}",
  "--format", "json", "--compact",
  "--output", "{output_file}",
]

[scanners.skillspector]
enabled = true
version = "批准的实际版本"
timeout_seconds = 600
command = [
  "skillspector", "scan", "{skill_root}",
  "--no-llm", "--format", "json",
  "--output", "{output_file}",
]
```

两套扫描器都是必需项，必须启用。命令参数会按参数数组执行，不经过 Shell 拼接。

`version` 应填写实际固定版本，不要填写 `configured`、`pin-in-deployment` 等占位值。

### 7.8 `[retry]` 与 `[concurrency]`

```toml
[retry]
max_attempts = 3
backoff_seconds = 5
max_backoff_seconds = 60

[concurrency]
repositories = 1
skills_per_repository = 1
ai_reviews = 1
```

首轮建议保持仓库并发为 1。当前命令按仓库人工驱动，以上数值不会自动把所有仓库并发跑完。

## 8. 标准操作流程

以下示例假设：

```text
配置文件：/secure/skill-review/review.toml
批次号：baseline-20260831
CSV：由配置文件指定
```

建议把批次号、操作人、配置文件 SHA-256 和执行时间记录到内部变更单或审查记录中。

### 步骤 1：校验配置

```bash
skill-batch-review validate-config /secure/skill-review/review.toml
```

需要机器读取时：

```bash
skill-batch-review validate-config /secure/skill-review/review.toml --json
```

该命令只解析配置，不访问 Gerrit，不启动扫描器。

成功示例：

```text
配置有效: /secure/skill-review/review.toml
扫描器: cisco, skillspector
质量门槛: 70/100
```

### 步骤 2：运行本地前置检查

```bash
skill-batch-review preflight /secure/skill-review/review.toml
```

该命令检查：

- CSV 是否存在；
- Gerrit 地址是否仍是示例值；
- 两套扫描器是否启用；
- 两套扫描器命令是否能在本机找到；
- 扫描器版本是否仍是占位值；
- AI Skill 和 JSON Schema 是否存在；
- AI 规则文件和结果 Schema 是否存在。

`preflight` 不验证 Gerrit 网络、SSH 权限、主机指纹和扫描器对真实样本的兼容性，这些需要在小批量联调中验证。

返回码：

```text
0  本地前置检查通过
2  存在阻断问题
```

### 步骤 3：生成批次清单

```bash
skill-batch-review init-batch \
  /secure/skill-review/review.toml \
  --batch-id baseline-20260831
```

默认输出：

```text
<manifest_root>/baseline-20260831/batch-manifest.json
```

临时覆盖 CSV：

```bash
skill-batch-review init-batch \
  /secure/skill-review/review.toml \
  --csv /secure/input/skills.csv \
  --batch-id baseline-20260831
```

只输出到标准输出：

```bash
skill-batch-review init-batch \
  /secure/skill-review/review.toml \
  --batch-id baseline-20260831 \
  --output -
```

该阶段仍然不联网、不扫描。

重点检查清单字段：

```text
inventory_csv_sha256
source_row_count
execution_record_count
exact_duplicate_count
input_conflict_count
records
```

### 步骤 4：生成仓库处理顺序

```bash
skill-batch-review plan-repositories \
  /secure/skill-review/review.toml \
  --output /secure/skill-review/repository-plan.json
```

重点查看：

```text
included_statuses
repositories_to_prepare
plans[].included_rows
plans[].excluded_rows
```

只有 `repositories_to_prepare` 中的仓库需要进入下一步。

建议在正式下载前人工确认：

- 仓库数量是否符合预期；
- 是否存在不应纳入的产品线；
- 状态映射是否造成大量误排除；
- 仓库白名单是否与计划一致；
- 同一仓库是否包含多个 Skill；
- 根目录 Skill 是否符合实际目录边界。

### 步骤 5：准备单个仓库

```bash
skill-batch-review prepare-repository \
  /secure/skill-review/review.toml \
  --batch-id baseline-20260831 \
  --repository team/repo-a
```

这个命令会真正访问 Gerrit 并运行两套静态扫描器。

执行过程：

```text
读取该仓库纳入状态的 CSV 行
→ 构造受控 SSH URL
→ 建立或更新一个 mirror
→ 冻结各分支版本
→ 校验 CSV Commit、分支、SKILL.md 和路径
→ 按 Skill 路径最近变化时间选择跨分支候选
→ 从固定 Revision 导出每个 Skill 快照
→ 计算完整内容 SHA-256
→ Cisco 与 SkillSpector 并行检查
→ 保存原始报告到受限证据区
→ 生成 AI handoff
→ 停止，等待人工执行 Claude Code
```

成功输出是 JSON，主要包含：

```json
{
  "repository_index": "/.../team-repo-a-xxxx.json",
  "task_count": 3,
  "conflict_count": 0,
  "ai_review_queue": [
    {
      "task_id": "task-...",
      "handoff": "/.../ai/handoff.json",
      "expected_result_filename": "task-....json",
      "invoke_skill": "/skill-security-review"
    }
  ],
  "model_invoked": false,
  "candidate_exported": false
}
```

必须保存 `repository_index` 路径，后续导入、报告和清理都会使用它。

如果 `task_count=0`，检查：

- 是否全部记录被状态过滤；
- CSV 是否过期；
- `SKILL.md` 是否已删除；
- 是否存在分支内容冲突；
- 快照是否存在 LFS、子模块、超限文件等覆盖阻断；
- 静态扫描是否失败。

这些情况会记录在仓库索引的来源记录、冲突或 `pre_ai_results` 中，不能简单当作“仓库没有问题”。

### 步骤 6：执行 Claude Code AI 审查

对 `ai_review_queue` 中每个任务分别处理。

#### 6.1 准备 Claude Code 环境

要求：

- 使用公司内网模型；
- 工作区中不放凭据；
- 只允许 `Read`、`Glob`、`Grep`；
- 禁止 Bash、写文件、网络、MCP 和子代理；
- 不执行目标 Skill 的任何内容；
- 不跟随离开 Skill Root 的链接。

AI Skill 自身声明：

```text
allowed-tools: Read Glob Grep
```

但这不代表运行环境已自动移除其他工具。执行人员仍需使用公司批准的 Claude Code 管理策略限制工具。

#### 6.2 调用 AI Skill

在 Claude Code 中调用：

```text
/skill-security-review
```

同时提供当前任务的 `handoff.json` 路径，并明确：

```text
按照 handoff 中的 Skill 根目录、Manifest、两份静态报告、固定 Revision、Digest、策略版本和模型标识完成只读审查。
最终只返回符合 review-result.schema.json 的一个 JSON 对象，不要输出 Markdown 代码块或额外说明。
```

#### 6.3 保存 AI 结果

为当前仓库创建独立目录：

```bash
mkdir -p /secure/skill-review/ai-results/team-repo-a
```

把 Claude Code 返回的纯 JSON 保存为：

```text
/secure/skill-review/ai-results/team-repo-a/<task_id>.json
```

文件名必须与 `expected_result_filename` 完全一致。

不要把多个 Skill 的结果放在一个 JSON 文件中，不要人工删改 Revision、Digest、模型或策略版本来绕过校验。

### 步骤 7：导入 AI 结果并形成候选

```bash
skill-batch-review finalize-repository \
  /secure/skill-review/review.toml \
  --batch-id baseline-20260831 \
  --repository-index /path/from/prepare/team-repo-a-xxxx.json \
  --ai-results-dir /secure/skill-review/ai-results/team-repo-a
```

程序会验证：

- 所有需要 AI 审查的 `task_id` 都有对应 JSON；
- JSON 符合 Schema；
- `review_id` 与任务一致；
- `source_revision` 与冻结版本一致；
- `skill_digest_sha256` 与快照一致；
- `policy_version` 与配置一致；
- 两份静态报告均存在并绑定相同 Digest；
- AI 文件覆盖数量和覆盖状态一致；
- 安全最高等级、质量维度总分和最终 disposition 自洽。

候选条件：

```text
快照完整
AND 两套静态检查完整
AND AI 结果有效
AND 安全结论为 PASS
AND 质量结论为 PASS
AND 质量分达到门槛
AND 不存在需要人工确认的特殊内容或分支冲突
```

成功输出包含：

```text
result_count
candidate_count
repository_result_index
commit_performed=false
push_performed=false
```

注意：`candidate_count=0` 不等于命令失败。可能是安全阻断、需要人工确认、检查不完整或质量分不足。

### 步骤 8：核对单仓库输出

至少检查：

- 仓库结果索引存在；
- 每个 AI 任务有受限证据目录；
- 原始 Cisco、SkillSpector 和 AI 结果没有进入候选目录；
- `BLOCK`、`REVIEW_REQUIRED`、`INCOMPLETE` 没有被导出为候选；
- 候选目录中的 `verified_digest` 与审查 Digest 一致；
- 候选中的来源仓库、分支、路径和 Revision 正确；
- 质量高分没有覆盖安全问题。

### 步骤 9：清理单仓库临时工作区

先确认结果索引和受限证据均已落盘，再执行：

```bash
skill-batch-review cleanup-repository \
  /secure/skill-review/review.toml \
  --batch-id baseline-20260831 \
  --repository team/repo-a \
  --repository-index /path/from/prepare/team-repo-a-xxxx.json \
  --confirm-cleanup
```

没有 `--confirm-cleanup` 时，命令会拒绝删除。

该命令只允许删除：

```text
<workspace.root>/<batch_id>/repositories/<当前仓库唯一目录>/
```

不会删除：

```text
evidence_root
candidate_root
manifest_root
原始 CSV
Git 远端内容
```

若 AI 任务没有全部形成持久化结果，清理会被阻止。

### 步骤 10：处理下一个仓库

从 `repository-plan.json` 的 `repositories_to_prepare` 选择下一项，重复步骤 5 至步骤 9。

首批小样本建议顺序：

1. 单 Skill、无脚本仓库；
2. 单 Skill、有脚本仓库；
3. 一个仓库多个 Skill；
4. 多分支相同路径；
5. 包含二进制、链接或 LFS 指针的特殊仓库。

### 步骤 11：生成批次报告

全部仓库完成后执行：

```bash
skill-batch-review report-batch \
  /secure/skill-review/review.toml \
  --batch-id baseline-20260831 \
  --results-dir <manifest_root>/baseline-20260831/repositories \
  --output-dir /secure/skill-review/reports/baseline-20260831
```

`results-dir` 中必须存在由 `finalize-repository` 生成的：

```text
*.results.json
```

固定输出：

```text
batch-summary.json
details.csv
failures.json
candidates.json
skill-security-review-report.html
```

`skill-security-review-report.html` 是可离线打开的管理报告，包含 Skill 清单、两套静态
检查与 AI 审查状态、安全结论、质量得分、风险分布、结果复用数量和脱敏问题明细。
被复用的 Skill 会显示 `RESULT_REUSED`、复用原因、原批次、原任务、内容比较方式和
“忽略时间戳”说明。原始报告和完整证据
不会嵌入 HTML，仍保留在受限证据区。

报告是脱敏汇总，不替代受限证据。

## 9. 输出目录详解

以下以 `baseline-20260831` 为例。

### 9.1 临时工作区

```text
<workspace.root>/baseline-20260831/repositories/
└── team-repo-a-<hash>/
    ├── mirror.git/
    ├── snapshots/
    │   └── <source_row_id>/
    └── tasks/
        └── <task_id>/
            └── scanner-output/
```

该目录在单仓库结果确认后可以清理。

### 9.2 受限证据区

```text
<evidence_root>/baseline-20260831/<task_id>/
├── package-manifest.json
├── source-resolution.json
├── scanners/
│   ├── cisco/
│   │   ├── raw-report.json
│   │   └── normalized-result.json
│   └── skillspector/
│       ├── raw-report.json
│       └── normalized-result.json
├── ai/
│   ├── handoff.json
│   └── imported-result.json
└── final-result.json
```

此目录可能包含完整风险证据和扫描器输出，必须限制访问。不要直接提交到普通 Git 仓库。

### 9.3 私密候选区

```text
<candidate_root>/
└── <repo-slug>/
    └── <skill-path-slug>/
        └── <skill_digest>/
            ├── package/
            │   ├── SKILL.md
            │   └── ...
            ├── source-manifest.json
            ├── review-summary.json
            └── export-verification.json
```

候选目录不包含原始扫描报告。后续是否手动同步到私密 Git 中转仓库，由负责人另行决定。

### 9.4 清单和结果索引

```text
<manifest_root>/baseline-20260831/
├── batch-manifest.json
└── repositories/
    ├── team-repo-a-<hash>.json
    ├── team-repo-a-<hash>.results.json
    ├── team-repo-b-<hash>.json
    └── team-repo-b-<hash>.results.json
```

普通仓库索引只保存必要的引用和脱敏摘要；完整 finding、扫描 stdout/stderr 和 AI 原始结果保存在受限证据区。

已通过结果的复用索引位于 `<manifest_root>/_approved-result-reuse/`。运行程序会先按 Skill
Root 目录名称定位候选，再使用快照阶段已有的整包 SHA-256 Digest 比对；时间戳不参与
摘要。只有安全和质量都通过、扫描器配置、策略、AI Skill、模型、Schema、质量门槛均
一致且原证据仍存在时，才跳过静态扫描和 AI 审查。名称相同但内容不同会正常重审。

## 10. 安全结论说明

| 结论 | 含义 | 是否进入候选 |
|---|---|---|
| `PASS` | 必需检查完整，没有阻断或待人工确认问题 | 还需质量通过 |
| `REVIEW_REQUIRED` | 存在高、中、不确定风险、特殊文件或分支内容冲突 | 否 |
| `BLOCK` / 报告中的 `BLOCKED` | 存在严重风险或明确禁止安装结论 | 否 |
| `INCOMPLETE` | 工具失败、报告缺失、覆盖不完整、CSV 过期或 Digest 不一致 | 否 |

质量结论：

| 质量分 | 等级 | 候选处理 |
|---|---|---|
| 90–100 | 优秀 | 安全通过时可进入候选 |
| 85–89 | 良好 | 安全通过时可进入候选 |
| 70–84 | 合格 | 安全通过时可进入候选 |
| 0–69 | 不合格 | 不进入候选 |
| 无分数 | 评分不完整 | 不进入候选 |

## 11. 常见错误和处理

### 11.1 `batch.included_statuses ...`

原因：

- 没有显式配置纳入状态；
- 配置的纳入状态不是 `status_mapping` 可能产生的值；
- 存在重复状态。

处理：确认真实 CSV 状态枚举，补齐映射后重新校验。不要把未知状态直接映射为 `ACTIVE`。

### 11.2 `unknown inventory status`

原因：CSV 出现了 `[status_mapping]` 未定义的状态。

处理：先确认该状态的业务含义，再决定映射和是否纳入。不要静默跳过。

### 11.3 `GERRIT_NOT_CONFIGURED`

原因：Gerrit 地址仍是 `gerrit.example.com` 等示例值。

处理：填写公司真实地址，并完成 SSH 主机指纹确认。

### 11.4 `SCANNER_NOT_FOUND`

原因：扫描器没有安装，或配置中的可执行文件路径不可用。

处理：使用批准的内网安装方式安装或填写绝对路径，再运行 `preflight`。

### 11.5 `SCANNER_VERSION_NOT_PINNED`

原因：扫描器版本仍是占位值。

处理：填写实际固定版本。版本变化应产生新的批次或任务版本，不能回写旧结论。

### 11.6 仓库不存在、权限不足或 SSH 失败

检查：

- `repo_name` 是否与 Gerrit 项目路径完全一致；
- `ssh_url_template` 是否需要 `.git`；
- 端口是否正确；
- 只读账号是否有目标项目权限；
- `known_hosts` 是否符合要求；
- 当前执行机是否位于公司网络。

不要改用带个人 Token 的公网 URL 临时绕过。

### 11.8 `STALE_INVENTORY`

含义：CSV Commit、当前分支、Skill 路径最近变化或 `SKILL.md` 实际状态无法对应。

处理：

1. 核查 CSV 生成时间；
2. 核查 `ref-update` 中 Commit 参数是否确实取更新后 Revision；
3. 核查 Skill 是否移动、删除或重命名；
4. 修正台账后建立新批次或重新生成输入。

不要把过期台账自动纠正后静默通过。

### 11.9 `BRANCH_CONTENT_CONFLICT`

含义：同一仓库和 Skill 路径在不同分支的最近变化时间相同，但内容 Digest 不同。

处理：分别保留两个候选，交由产品线或指定人员确认本轮应审查和归档哪一版。脚本不会静默任选。

### 11.10 快照覆盖不完整

常见原因：

- Git LFS 指针；
- 子模块；
- 超大文件；
- 包总大小或文件数超限；
- Blob 无法读取；
- 缺少 `SKILL.md`。

处理：补齐实际内容或调整经过批准的限制后重新生成快照。覆盖不完整不能标记为安全通过。

### 11.11 静态扫描失败或超时

结果会进入 `INCOMPLETE`，不会生成正常 AI 任务或候选。

检查工具版本、输入格式、超时、输出路径和报告大小。重试必须保留失败证据，不能覆盖为一次看似干净的新结果。

### 11.12 AI JSON 校验失败

常见原因：

- 输出包含 Markdown 代码块或额外说明；
- 文件名不是 `<task_id>.json`；
- `review_id`、Revision、Digest 或策略版本不一致；
- 缺少静态报告覆盖记录；
- 质量维度之和不等于总分；
- 安全结论和 overall disposition 冲突；
- AI 跳过文件却返回 `PASS`。

处理：使用原 handoff 重新执行 AI 审查，不要手工篡改冻结字段来通过校验。

### 11.13 清理被阻止

常见原因：

- 缺少 `--confirm-cleanup`；
- 仓库结果索引不存在；
- 仍有 AI 任务未形成最终结果；
- 仓库索引与清理目标不一致；
- 目标不是配置下的准确仓库工作区。

先补齐结果和证据，不要直接使用宽泛的递归删除命令。

## 12. 中断与恢复建议

### 在 `prepare-repository` 前中断

没有远端或扫描副作用，可以重新执行本地计划命令。

### 在仓库下载或扫描期间中断

保留当前仓库工作区和受限证据，不要先清理。检查仓库索引和各任务证据是否完整，再决定重跑当前仓库。

### 在等待 AI 审查时中断

保留：

```text
repository_index
evidence_root/<batch_id>/<task_id>/ai/handoff.json
workspace.root 中的对应 Skill 快照
```

下次继续完成缺少的 `<task_id>.json`，再运行 `finalize-repository`。

### 在候选导出后中断

候选导出带 Digest 校验，相同内容的安全重试是幂等的。不要删除已经形成的受限证据。

## 13. 首轮小批量验收清单

正式处理几百个 Skill 前，建议选择 2～5 个仓库并逐项确认：

- [ ] CSV 原始 SHA-256 已记录；
- [ ] CSV 识别编码已记录且中文、仓库名和 Skill 路径显示正常；
- [ ] 状态映射经过业务确认；
- [ ] Gerrit 使用只读 SSH；
- [ ] 仓库白名单仅包含试运行目标；
- [ ] 两套扫描器版本已经固定；
- [ ] 两套扫描器不联网、不调用公网模型；
- [ ] 同一仓库只建立一个 mirror；
- [ ] 每个 Skill 使用固定 Revision 快照；
- [ ] 两套扫描器绑定相同 Digest；
- [ ] Claude Code 只开放读取工具；
- [ ] AI 结果通过 Schema 和冻结字段校验；
- [ ] 安全结论与质量得分分开；
- [ ] 严重、高、中风险没有被质量高分抵消；
- [ ] 原始报告只在受限证据区；
- [ ] 候选目录不包含原始报告；
- [ ] 候选导出后 Digest 一致；
- [ ] 脚本没有 Commit、Push 或上架候选；
- [ ] 清理只删除当前仓库临时工作区；
- [ ] 四类批次报告可以生成；
- [ ] 异常和人工确认事项可回查。

## 14. 测试与开发验证

在仓库根目录执行：

```bash
PYTHONPATH=batch-review/src \
python3 -W error -m unittest discover -s batch-review/tests -v
```

验证 AI 审查 Skill：

```bash
python3 /path/to/skill-creator/scripts/quick_validate.py \
  .claude/skills/skill-security-review
```

验证 JSON Schema 语法：

```bash
python3 -m json.tool \
  .claude/skills/skill-security-review/references/review-result.schema.json \
  >/dev/null
```

当前项目本地测试包含：CSV、配置、Git 来源解析、快照、扫描器、AI 校验、安全策略、证据、候选、状态、报告、命令行和单仓库两阶段集成流程。

## 15. 一页式执行清单

```text
1. 填写 review.toml
2. validate-config
3. preflight
4. init-batch
5. plan-repositories
6. 人工确认仓库计划
7. prepare-repository（一个仓库）
8. 按 ai_review_queue 调用 /skill-security-review
9. 保存 <task_id>.json
10. finalize-repository
11. 核对证据、结论和候选
12. cleanup-repository --confirm-cleanup
13. 处理下一个仓库
14. 全部完成后 report-batch
15. 人工决定是否将候选同步到私密 Git 中转仓库
```

执行过程中如遇到来源不一致、扫描失败、覆盖不完整或 AI 校验失败，应保留证据并停止该 Skill 的候选导出，不能通过人工修改状态绕过门禁。
