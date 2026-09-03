# Skill 安全审查项目初始化与引导式操作说明

## 1. 目标

执行人员不再手工复制模板、记录批次号或记忆 `plan`、`start`、`advance` 的参数。项目使用
一个本机配置、一个本机操作状态和一个统一入口判断下一步。

```text
首次初始化
→ 填写一次真实配置
→ 安装一次静态扫描器
→ 自动建立批次
→ 逐个下载并审查 Skill
→ AI 检查点
→ 保存结果并进入下一项
→ 输出批次 CSV/JSON
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

1. 自动选择 Python 3.11～3.14；安装扫描器时要求 Python 3.12～3.14；
2. 让执行人员选择公司 Gerrit 或 GitHub 验证环境；
3. 从对应模板生成 `batch-review/config/review.local.toml`；
4. 将临时目录和扫描器目录调整成本机路径；
5. 保存本机配置位置，但不保存密码、Token 或私钥内容；
6. 告知尚需填写的配置和下一入口。

本机配置和状态都位于被 Git 忽略的目录。默认不会覆盖已经存在的配置。只有明确使用
`--force` 的运维操作才允许重新生成配置；双击入口不会使用该参数。

## 3. 只需记住一个日常入口

初始化完成后：

- Windows 双击 `batch-review\review.cmd`；
- Linux/CentOS 执行 `./batch-review/review.sh`。

入口会先进行只读状态检查，然后根据实际情况执行一种动作：

| 当前状态 | 入口行为 |
|---|---|
| 配置未完成 | 显示唯一配置文件和待填写字段，不执行网络操作 |
| 扫描器未安装 | 经确认后从当前公司 pip 源安装固定版本工具 |
| 尚无批次 | 经确认后生成批次计划，不联网、不扫描 |
| 计划已生成 | 经确认后下载并扫描第一个 Skill |
| 等待 AI | 提醒在 Claude Code 使用 `/ask-cc` |
| AI 结果已存在 | 经确认后保存结果、清理本次临时目录并进入下一项 |
| 内容结果可复用 | 经确认后记录复用关系并进入下一项 |
| 批次完成 | 显示结果 CSV 和 JSON 的绝对路径 |

安装、联网扫描和清理前仍保留一次简单确认，避免双击误操作。配置、批次号、当前 Skill 和
输出路径由本机状态自动读取。

## 4. ask-cc

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
定位当前 `ai-review-current.json`，再指向专门的 `/skill-security-review`。两者职责不同：

- `/ask-cc`：判断项目走到哪里以及下一步是什么；
- `/skill-security-review`：只读审查一个已经准备好的 Skill，并产生固定 JSON。

## 5. 本机文件

```text
batch-review/config/review.local.toml
    当前机器实际使用的配置

batch-review/.batch-review/operator-state.json
    当前配置路径和批次号

<manifest_root>/<batch_id>/per-skill-launcher-state.json
    批次和逐 Skill 进度

<manifest_root>/<batch_id>/ai-review-current.json
    当前 AI 审查交接位置
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
