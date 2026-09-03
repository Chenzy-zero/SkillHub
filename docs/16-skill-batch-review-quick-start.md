# Skill 批量安全审查快速使用说明

本文用于第一次配置和日常逐 Skill 执行。完整字段、输出结构和故障处理见
[`15-skill-batch-review-script-user-guide.md`](./15-skill-batch-review-script-user-guide.md)。

## 1. 运行方式

### 推荐：首次初始化后使用免参数入口

Windows 首次双击 `batch-review\init.cmd`，之后始终双击 `batch-review\review.cmd`。
Linux/CentOS 首次执行 `./batch-review/init.sh`，之后始终执行 `./batch-review/review.sh`。

初始化入口会创建真实的本机配置 `batch-review/config/review.local.toml`；该文件被 Git 忽略，
已有文件默认不会被覆盖。`review` 入口会自动记住配置文件和批次号，根据真实状态提示并执行
当前唯一的下一步。Claude Code 中可输入 `/ask-cc` 查看相同状态，无需记忆命令、路径和参数。

下面的参数化命令主要用于理解流程和运维排障。

整套流程不能只用一个 AI Skill 完成，职责分为两部分：

```text
run.sh / run.cmd
  ├─ 读取 CSV，逐 Skill 执行 Blobless Partial Fetch
  ├─ 只导出目标 Skill 到 skills/<skill_id>/<skill_name>
  ├─ 运行 Cisco AI Skill Scanner 与 SkillSpector
  ├─ 生成 AI 审查交接文件
  ├─ 校验 AI 结果，生成单项 JSON 和批次 CSV/JSON
  └─ 确认结果持久化后清理当前 Skill 的 git_download

/skill-security-review
  └─ 只负责读取一个交接文件和 Skill 快照，输出一份 AI 审查 JSON
```

AI Skill 不下载仓库、不调用静态扫描器、不清理目录，也不执行被审查 Skill。

## 2. 填写配置

在仓库根目录执行：

```bash
cp batch-review/config/review.company.example.toml \
  batch-review/config/review.company.toml
```

使用推荐初始化入口时无需手工执行上述复制命令，程序会自动创建
`batch-review/config/review.local.toml` 并告诉执行人员还需要填写哪些字段。

真实配置文件已被 `.gitignore` 忽略。至少填写下列内容：

| 配置项 | 填写内容 |
|---|---|
| `batch.inventory_csv` | Skill 清单路径；仓库内测试清单默认是 `test/skill_summary.csv` |
| `workspace.*` | 临时区、受限证据区、清单区、`git_download_root`、`skills_root` 和 `results_root` 的绝对路径 |
| `gerrit.user/host/port` | Gerrit 只读 SSH 参数 |
| `gerrit.allowed_repositories` | 首批联调的 1～3 个仓库；正式批次确认后再放开 |
| `status_mapping` | CSV 中各状态对应 `ACTIVE`、`DELETED` 等内部状态 |
| `scanners.*.version` | 公司内网源实际批准的固定版本 |
| `scanners.*.command[0]` | 两个扫描器可执行文件的绝对路径 |

配置中不要填写 Git 密码、SSH 私钥、模型密钥或 pip 密码。认证信息使用服务器已有的
SSH 配置、环境变量或公司的密钥管理方式提供。

CSV 可直接使用 UTF-8、UTF-8 BOM、带 BOM 的 UTF-16 或 GBK/GB18030，无需在 Excel 中
另存转换。程序不会修改源文件，并会在批次记录中保存识别编码和原始 SHA-256。

## 3. 安装

批处理程序支持 Python 3.11～3.14；扫描器安装支持 Python 3.12～3.14。Windows 和 Linux
入口会自动选择可用兼容版本。CentOS 7.9 使用 Python 3.14 前应确认内网源具备兼容 wheel。

先从公司的 pip 内网源安装批处理程序：

```bash
python3.12 -m pip install -e './batch-review'
```

在公司内网源已经同步两个固定版本 wheel 后，安装静态扫描器：

```bash
python3.12 batch-review/tools/install_scanners.py \
  --root /opt/skill-review/scanners
```

扫描节点不能访问 GitHub也不使用 Docker。安装脚本先从当前 pip 配置的内网源安装固定版
`uv==0.12.9`，再用 uv 解析两个固定版本扫描器并建立独立环境。这样可规避 pip 的
`resolution-too-deep`，不需要手动指定间接依赖版本。安装完成后，把输出的可执行文件路径
填回配置。

## 4. 首次检查，不联网、不扫描

Linux/CentOS：

```bash
./batch-review/run.sh plan \
  --config batch-review/config/review.company.toml \
  --batch-id baseline-20260901
```

Windows PowerShell 或 CMD：

```text
batch-review\run.cmd plan --config batch-review\config\review.company.toml --batch-id baseline-20260901
```

`plan` 只读取配置和 CSV，并在 `workspace.manifest_root` 下生成本地执行计划。它不访问
Gerrit，也不运行扫描器。计划中的 Skill 数、排除项和状态应先人工核对。逐 Skill 模式要求
每个纳入行具有唯一且非空的 `skill_id`。

## 5. 启动第一个 Skill

`start` 会访问 Gerrit 并实际运行两套静态扫描器，因此必须显式写 `--execute`：

```bash
./batch-review/run.sh start \
  --config batch-review/config/review.company.toml \
  --batch-id baseline-20260901 \
  --execute
```

脚本会使用 partial fetch 一次只获取一个 Skill，并生成：

- `ai-review-current.json`：当前 Skill 的 handoff 和结果保存路径；
- 静态扫描报告、Skill 摘要和冻结版本信息。

`start` 会继续使用 `plan` 已生成的同一批次和固定配置；如果没有预先执行 `plan`，它也会先
创建计划再准备第一个 Skill。批次创建后不得修改该批次使用的配置文件；配置变化应新建批次。

## 6. 调用 AI 审查 Skill

在本仓库根目录启动公司批准的 Claude Code。输入触发指令：

```text
/skill-security-review
```

然后为队列中的每个任务提供以下要求：

```text
读取 ai-review-current.json 中的 handoff JSON。
严格按 handoff、项目 Skill 和结果 Schema 审查；不要联网，不要执行被审查内容，
不要修改快照。只返回 JSON，并将结果保存到该任务的 expected_result 路径。
```

每个任务必须生成一份独立 JSON。不能把聊天说明、Markdown 代码围栏或多个结果混入同一
文件；批处理程序会校验 Schema、Digest、规则版本和任务身份，不接受不匹配的结果。

## 7. 完成当前 Skill 并进入下一个 Skill

确认当前队列的所有 AI JSON 已保存后执行：

```bash
./batch-review/run.sh advance \
  --config batch-review/config/review.company.toml \
  --batch-id baseline-20260901 \
  --execute \
  --confirm-cleanup
```

该命令按以下顺序执行：

1. 检查当前 Skill 的 AI 结果是否齐全；
2. 校验并合并静态结果与 AI 结果；
3. 在 `skills/<skill_id>/review-result.json` 写入单项结果；
4. 更新 `results/<batch_id>/` 下的 CSV 和 JSON；
5. 清理当前 Skill 的 `git_download` 临时目录；
6. 下载并准备下一个 Skill。

若 AI 结果缺失，命令停止并列出缺失路径，不会清理、不会跳过，也不会进入下一个 Skill。
每完成一次 AI 审查就再次执行同一条 `advance` 命令，直至批次完成。HTML 视觉改版后续进行，
本阶段以单项 JSON 和批次 CSV/JSON 为正式输出。

## 8. 查看进度

```bash
./batch-review/run.sh status \
  --config batch-review/config/review.company.toml \
  --batch-id baseline-20260901
```

Windows 将 `./batch-review/run.sh` 替换为 `batch-review\run.cmd`。入口会依次查找 Python
3.14、3.13、3.12 和 3.11；如需固定解释器，可设置 `SKILL_REVIEW_PYTHON` 为完整路径。

## 9. 一键启动的边界

`start` 是第一个 Skill 的单命令启动入口，`advance` 是每个后续 Skill 的单命令续跑入口。两者
之间必须由公司内网模型完成 AI 审查，因此不能安全地做成完全无人值守的一条命令。这个暂停点
用于防止 AI 结果缺失、结果串批或未经确认就删除临时证据。

程序不会自动 Commit、Push 或上架 SkillHub。全部纳管内容写入 `skills_root`，安全通过与否
由各 `skill_id` 下的 JSON 及批次结果表表示；后续同步动作仍由负责人决定。

## 10. 使用 GitHub 仓库做联调

先从远端已固定的 Git Revision 生成台账，不读取本地未提交内容：

```bash
python3.12 batch-review/tools/discover_git_skills.py \
  --repository . \
  --repo-name Chenzy-zero/SkillHub \
  --branch main \
  --revision origin/main \
  --output test/github_skill_summary.csv
```

复制 `batch-review/config/review.github.example.toml` 为一个 `*.local.toml` 文件，然后填写：

- GitHub 或正式 Gerrit 的 SSH 地址、端口、只读身份和仓库白名单；
- Cisco 与 SkillSpector 可执行文件路径；
- AI 审查 Skill 和结果 Schema 的路径。

策略版本由审查规则内容自动计算；模型由 Claude Code 会话记录，无需填写。

GitHub 受限网络可使用 `ssh.github.com:443`。正式切换 Gerrit 时只需替换源站段、CSV、
工作目录和工具路径，台账字段及后续命令不变。`*.local.toml` 已被 Git 忽略。
