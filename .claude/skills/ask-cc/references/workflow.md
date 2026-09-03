# 状态与下一步对照

| `next_action` | 含义 | 告诉用户的动作 |
|---|---|---|
| `INITIALIZE` | 尚无本机配置和操作状态 | Windows 双击 `batch-review/init.cmd`；Linux/CentOS 执行 `batch-review/init.sh` |
| `EDIT_CONFIG` | 配置缺失、示例值未替换或 CSV 不可用 | 打开状态输出中的唯一 `config_path`，修正列出的问题 |
| `INSTALL_SCANNERS` | 配置正常，但固定版本扫描器不存在 | 双击或执行 `batch-review/review.cmd`/`review.sh` 并确认安装 |
| `PLAN` | 可以创建批次 | 双击或执行 `review` 入口并确认生成计划；该步不联网 |
| `START` | 计划已建立 | 双击或执行 `review` 入口并确认下载和静态扫描第一个 Skill |
| `AI_REVIEW` | 静态扫描完成，缺少当前 AI JSON | 核对 `ai-review-current.json` 后调用 `/skill-security-review` |
| `ADVANCE` | AI JSON 已存在，或当前结果可以复用 | 双击或执行 `review` 入口，确认持久化、清理和进入下一项 |
| `VIEW_RESULTS` | 批次完成 | 展示状态输出中的 CSV/JSON 结果路径 |
| `MANUAL_CHECK` | 状态损坏或无法安全判断 | 报告状态文件和错误，不执行修复或清理 |

正常顺序：

```text
首次初始化
→ 填写一次本机配置
→ 安装一次扫描器
→ 自动生成批次计划
→ 下载并静态扫描一个 Skill
→ 同内容复用，或调用 /skill-security-review
→ 自动持久化和准备下一项
→ 重复到批次完成
→ 查看批次 CSV/JSON
```
