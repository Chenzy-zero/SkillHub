# 状态与下一步对照

| `next_action` | 含义 | 告诉用户的动作 |
|---|---|---|
| `INITIALIZE` | 尚无本机配置和操作状态 | Windows 双击 `batch-review/init.cmd`；Linux/CentOS 执行 `batch-review/init.sh` |
| `EDIT_CONFIG` | 配置缺失、示例值未替换或 CSV 不可用 | 打开状态输出中的唯一 `config_path`，修正列出的问题 |
| `INSTALL_SCANNERS` | 配置正常，但固定版本扫描器不存在 | 双击或执行 `batch-review/review.cmd`/`review.sh` 并确认安装 |
| `PLAN` | 可以创建批次 | 双击或执行 `review` 入口并确认生成计划；该步不联网 |
| `START` | 计划已建立 | 在 Claude Code 调用 `/auto-skill-review`，由脚本下载并完成当前仓库全部静态扫描 |
| `AI_REVIEW` | 当前仓库静态扫描完成，等待 AI 队列 | 核对 `ai-review-queue.json` 后调用 `/auto-skill-review` |
| `ADVANCE` | AI JSON 已存在，或当前结果可以复用 | 调用 `/auto-skill-review`，由脚本批量持久化、清理并进入下一仓库 |
| `VIEW_RESULTS` | 批次完成 | 展示状态输出中的 CSV/JSON 结果路径 |
| `MANUAL_CHECK` | 状态损坏或无法安全判断 | 报告状态文件和错误，不执行修复或清理 |

正常顺序：

```text
首次初始化
→ 填写一次本机配置
→ 安装一次扫描器
→ 自动生成批次计划
→ 按仓库下载并逐一静态扫描全部 Skill
→ 为每个待审 Skill 启动独立 Agent
→ 自动批量持久化、清理并准备下一仓库
→ 重复到批次完成
→ 查看批次 CSV/JSON
```
