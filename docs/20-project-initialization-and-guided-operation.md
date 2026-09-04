# Skill 安全审查项目初始化与引导式操作说明

## 1. 目标

执行人员不再手工复制模板、记录批次号或记忆 `plan`、`start`、`advance` 的参数。项目使用
一个本机配置、一个本机操作状态和一个统一入口判断下一步。

```text
首次初始化
→ 填写一次真实配置
→ 安装一次静态扫描器
→ 在 Claude Code 调用 /auto-skill-review
→ 脚本自动建立批次、按仓库下载并提取全部台账 Skill
→ 逐一完成静态扫描并生成当前仓库 AI 队列
→ 独立 Agent 批量审查并自动保存结果
→ 清理后进入下一仓库，输出批次 CSV/JSON
```

## 2. 首次初始化

### Windows

双击：

```text
batch-review\init.cmd
```

### Linux/CentOS

```bash
./batch-review/init.sh
```

初始化程序执行以下操作：

1. 主程序自动选择 Python 3.11～3.14；安装 SkillSpector 时自动选择具有官方
   `yara-python` wheel 的 Python 3.12 或 3.13；Windows 64 位机器如果没有，会在得到用户安装确认后
   自动部署仓库内的 Python.org 3.13.15 到 `.scanner-tools`；
2. 让执行人员选择公司 Gerrit 或 GitHub 验证环境；
3. 从对应模板生成 `batch-review/config/review.local.toml`；
4. 将临时目录和扫描器目录调整成本机路径；
5. 保存本机配置位置，但不保存密码、Token 或私钥内容；
6. 告知尚需填写的配置和下一入口。

本机配置和状态都位于被 Git 忽略的目录。默认不会覆盖已经存在的配置。只有明确使用
`--force` 的运维操作才允许重新生成配置；双击入口不会使用该参数。

## 3. 只需记住一个日常入口

初始化完成并通过扫描器健康检查后，可以直接在 Claude Code 输入 `/auto-skill-review`。
Windows 的 `batch-review\review.cmd`、Linux/CentOS 的 `./batch-review/review.sh` 仍可作为
手动排障入口。

入口会先进行只读状态检查，然后根据实际情况执行一种动作：

| 当前状态 | 入口行为 |
|---|---|
| 配置未完成 | 显示唯一配置文件和待填写字段，不执行网络操作 |
| 扫描器未安装 | 经确认后从当前公司 pip 源安装固定版本工具 |
| 尚无批次 | 由 `/auto-skill-review` 调用脚本生成计划并启动仓库级静态扫描 |
| 计划已生成 | 下载当前仓库一次，提取并静态扫描其全部台账 Skill |
| 等待 AI | 由 `/auto-skill-review` 为队列批量启动独立 Agent |
| AI 结果已存在 | 自动保存全部结果；本仓库完成后清理并进入下一仓库 |
| 内容结果可复用 | 自动记录复用关系并继续 |
| 批次完成 | 显示结果 CSV 和 JSON 的绝对路径 |

首次安装和首次联网运行保留一次确认。批次启动后，配置、批次号、当前 Skill、仓库和输出路径
均从本机状态读取，不再为每一步重复停顿。

## 4. Claude Code 自动审查

在本项目的 Claude Code 中输入：

```text
/auto-skill-review
```

该 Skill 会先调用 `batch-review` 受信脚本完成计划、下载和静态扫描，再读取当前仓库 AI 队列，
按 `skill-security-review` 的只读规则为每项启动独立 Agent，把 JSON 写入唯一约定位置，然后
自动合并结果、清理并推进下一仓库，直到批次完成或遇到真实阻塞。它不会发布、Push、安装或
执行被审查内容。

`/ask-cc` 继续作为只读状态查询入口：

项目级 Skill 位于：

```text
.claude/skills/ask-cc/
```

在本项目的 Claude Code 中输入：

```text
/ask-cc
```

它会调用只读状态检查器，并使用固定格式回答：

```text
当前状态
当前对象
需要处理
下一步
```

`ask-cc` 不执行初始化、下载、扫描、安装、清理或 Git 操作。当状态为等待 AI 审查时，它会
定位当前仓库的 `ai-review-queue.json`（旧批次才回退到 `ai-review-current.json`），并指向自动入口。
三个 Skill 的职责不同：

- `/ask-cc`：判断项目走到哪里以及下一步是什么；
- `/skill-security-review`：只审查一个已经准备好的 Skill Package，并产生固定 JSON；不读取静态扫描报告。
- `/auto-skill-review`：以轻量主会话调度脚本和 AI 队列，每个 Skill 使用一个新的独立 Agent 上下文，避免多个 Skill 的内容累积到同一上下文。

## 5. 本机文件

```text
batch-review/config/review.local.toml
    当前机器实际使用的配置

batch-review/.batch-review/operator-state.json
    当前配置路径和批次号

<manifest_root>/<batch_id>/per-skill-launcher-state.json
    批次和逐 Skill 进度

<manifest_root>/<batch_id>/ai-review-queue.json
    当前仓库全部 AI 审查队列

<manifest_root>/<batch_id>/ai-review-current.json
    兼容旧入口的队列首项
```

这些本机状态不会提交到 Git。原始 CSV 不会被修改，批次状态保存 CSV 的 SHA-256；批次创建后
配置或 CSV 发生变化时，底层启动器会停止，防止不同批次数据混用。

## 6. 运维和故障排查入口

普通执行不需要下面的命令。需要自动化接入或排查时，可以直接读取状态：

```bash
./batch-review/review.sh
```

需要机器可读状态时，可使用任意兼容解释器运行
`batch-review/tools/project_status.py --json`。

原来的 `run.sh`、`run.cmd` 和 `run_skill_batch.py` 保留，不改变已有自动化接口。
