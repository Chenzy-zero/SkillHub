# Windows 下使用 Codex CLI 或 Claude Code 执行 Skill 批量安全审查

## 1. 最终执行方式

Windows 上只维护一套批量审查流程。Python 脚本负责所有可以稳定重复的工作，Codex CLI
或 Claude Code 只负责两件事：调用受信脚本，以及让隔离的审查 Agent 阅读单个 Skill。

```text
CSV 清单
  → Python 按 repo_name + branch 分组
  → 当前仓库只下载一次无历史归档
  → 提取该仓库在 CSV 中登记的全部 Skill
  → 逐 Skill 执行 Cisco 与 SkillSpector 静态扫描
  → 生成当前仓库 AI 队列
  → 每个 Skill 分配一个独立审查 Agent
  → Python 校验 AI JSON 并合并静态结果
  → 写单项结果和批次 CSV/JSON
  → 清理当前仓库临时目录
  → 自动进入下一仓库
```

两种 AI 客户端使用同一个规则目录：

```text
batch-review/skills/skill-security-review/
```

`.agents/`、`.codex/` 和 `.claude/` 只解决客户端发现、触发语法和审查 Agent 隔离问题，
不保存第二份安全规则。

## 2. Windows 首次使用

在仓库根目录双击：

```text
batch-review\init.cmd
```

初始化会生成 `batch-review/config/review.local.toml`。只需在这个文件中核对公司 Gerrit
只读连接、CSV 路径、仓库白名单和扫描器路径；已有配置不会被覆盖。

然后双击一次：

```text
batch-review\review.cmd
```

首次安装扫描器会访问已配置的公司 pip 源，因此仍需要人工确认。安装结束必须生成
`.scanner-tools/scanner-health.json`，且 Cisco 与 SkillSpector 的离线真实扫描均通过。
这一步完成后，日常批次不再要求反复打开 `review.cmd`。

主程序可以使用 Python 3.11～3.14。Windows 上的 Cisco 可以使用 Python 3.14；
SkillSpector 使用项目私有 Python 3.13，这是其二进制依赖 wheel 的兼容要求，不会修改
系统 PATH，也不会替换操作人员已有的 Python 3.14。

## 3. 日常一键执行

从仓库根目录启动一个客户端，不要在 `batch-review/` 子目录外另建工作区。

### Codex CLI

```text
$auto-skill-review
```

Codex 从 `.agents/skills/auto-skill-review/` 发现入口，并使用
`.codex/agents/skill_security_reviewer.toml` 为每个 Skill 建立独立上下文。

### Claude Code

```text
/auto-skill-review
```

Claude Code 从 `.claude/skills/auto-skill-review/` 发现入口，并使用
`.claude/agents/skill-security-reviewer.md` 为每个 Skill 建立独立上下文。

两种入口都会在 Windows 中通过以下受信命令推进状态：

```text
cmd.exe /d /c "batch-review\status.cmd --json"
cmd.exe /d /c "batch-review\review.cmd --auto"
```

不要同时用两个客户端推进同一批次。需要切换客户端时，先确认前一个客户端已经停止，
再由另一个客户端读取现有状态继续。

## 4. 上下文和权限边界

协调会话只读取状态 JSON 和 AI 队列中的任务路径，不读取以下内容：

- 被审查 Skill 文件；
- 静态扫描原始报告和规范化报告；
- package manifest；
- 已有 AI 报告和批次证据。

每个审查 Agent 只接收 `task_id`、`handoff`、`expected_result`，并且只读取自己的
`skill_root`、统一规则和结果 Schema。它不得执行、导入、安装或联网访问被审查内容，
只能向自己的 `expected_result` 写一份 JSON。

静态报告不交给 AI。Python 在 AI 结果落盘后独立完成 Schema、Digest、Revision、策略版本
和任务身份校验，再合并两套静态扫描结论。这样父会话不会因为读取数百份报告而消耗大量
上下文，也避免扫描器文字影响 AI 对 Skill 本身的独立判断。

## 5. 当前已排除的问题

| 检查项 | 当前处理 |
|---|---|
| Codex CLI 找不到项目 Skill | 已增加 `.agents/skills/` 项目入口 |
| 两个客户端各有一份审查规则 | 已统一到 `batch-review/skills/skill-security-review/` |
| Claude 与 Codex 触发语法不同 | 队列同时记录 `/skill-security-review` 和 `$skill-security-review` |
| 父会话读取报告导致上下文膨胀 | 协调 Skill 明确禁止读取报告和 Skill；单项内容交给隔离 Agent |
| Windows Bash 不能直接可靠启动 `.cmd` | AI 入口统一通过 `cmd.exe /d /c` 调用 |
| Windows 管道中文乱码 | 四个 `.cmd` 入口固定 UTF-8 code page、`PYTHONUTF8` 和 `PYTHONIOENCODING` |
| 本机旧配置仍指向 `.claude` 规则目录 | 配置加载器只对仓库内旧标准路径自动映射到统一规则，不改写本机配置 |
| 批次期间规则变化导致结果混用 | 新批次冻结 `ai_policy_version`；规则不一致时要求保留旧证据并新建批次 |
| AI 结果直接决定最终结论 | Python 先严格校验，再与两套静态结果合并 |

## 6. 仍需在正式 Windows 机器确认的事项

以下事项依赖公司环境，仓库内测试无法替代：

1. 公司 pip 源包含 Cisco 固定版本和 SkillSpector 运行依赖的 Windows wheel；安装后的
   `scanner-health.json` 必须为当前工具目录生成，不能从其他机器复制。
2. Gerrit 账号有 `ls-remote` 和 `git archive --remote` 只读权限。目标 Gerrit 不支持
   partial clone 和按路径远程 archive，当前程序已经按“一仓一次整仓无历史归档”适配。
3. Codex CLI 或 Claude Code 对工作区的写权限包含配置中的 `manifest_root`、`skills_root`
   和 `results_root`。如果这些目录位于仓库外受限盘符，需要在客户端启动时批准该工作区。
4. 客户端版本必须支持项目级 Skill 和项目级审查 Agent。若隔离 Agent 不可用，协调入口
   会停止并报告 `CONTEXT_ISOLATION_UNAVAILABLE`，不会退回父会话直接审查。
5. `[concurrency].ai_reviews` 应不超过客户端允许的并发数。静态扫描和仓库状态仍由脚本
   串行控制，不能通过多开窗口同时推进一个批次。

## 7. 快速状态检查

不启动扫描，只查看当前状态：

```text
Codex CLI：$ask-cc
Claude Code：/ask-cc
```

也可以直接执行：

```text
batch-review\status.cmd --json
```

状态为 `INITIALIZE`、`EDIT_CONFIG` 或 `INSTALL_SCANNERS` 时，需要先完成唯一提示的人工
操作；状态为 `PLAN`、`START`、`AI_REVIEW` 或 `ADVANCE` 时，重新调用自动入口即可；状态为
`COMPLETE` 时，结果路径由状态输出给出。

## 8. 参考的客户端约定

- Codex 项目 Skill 与显式 `$skill-name` 调用：<https://learn.chatgpt.com/docs/build-skills>
- Codex 项目自定义审查 Agent：<https://learn.chatgpt.com/docs/agent-configuration/subagents>
- Codex Windows 沙箱：<https://learn.chatgpt.com/docs/windows/windows-sandbox>
- Claude Code 项目 Skill：<https://code.claude.com/docs/en/slash-commands>
- Claude Code 项目 subagent：<https://code.claude.com/docs/en/sub-agents>
