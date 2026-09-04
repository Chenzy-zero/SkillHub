# 逐 Skill 安全审查实施任务

## 1. 任务清单

| 编号 | 任务 | 主要输出 | 完成标准 |
|---|---|---|---|
| S01 | 输入契约 | 必需 CSV 字段与 `skill_id` 校验 | 正式 CSV 可读取；任意扩展字段允许并原样保留；缺少必需字段时明确报错 |
| S02 | 目录配置 | `git_download_root`、`skills_root`、`results_root` | 目录互不危险重叠；清理目标受限 |
| S03 | 仓库级下载 | 每个仓库分支一次整仓无历史归档 | 同组多个 Skill 只调用一次 archive；不含 `.git` 和历史 |
| S04 | 单 Skill 导出与迁移 | `skills/<skill_id>/<skill_name>/` | 无 `.git`；Manifest/Digest 一致；不覆盖冲突内容 |
| S05 | 仓库/Skill 状态机 | 仓库串行、组内 Skill 逐一静态扫描并形成 AI 队列 | 同仓库全部 AI 结果完成后自动进入下一仓库 |
| S06 | 静态与 AI 审查接入 | 单项 handoff、批量 AI 队列与最终结论 | 扫描目标为永久 Skill 目录；绑定固定 Digest；每项 AI 使用独立 Agent |
| S07 | 内容复用 | `content_id` 与 `RESULT_REUSED` | 同名同内容跳过审查；当前来源结果独立生成 |
| S08 | 单项 JSON | `review-result.json` | 来源、问题、结论、复用和证据字段完整 |
| S09 | 批次结果表 | CSV 与 JSON | 每项完成后原子更新；可中断恢复 |
| S10 | 清理 | 安全清理当前仓库 `git_download` | 只删除当前仓库目录，不删除 Skill、证据和结果 |
| S11 | 自动入口与 Skill | `review --auto`、`/auto-skill-review`、受控 Agent 并发 | 无需先手动打开 review；自动续跑到完成或真实阻塞 |
| S12 | 测试 | 单元与集成测试 | 覆盖下载限制、目录、复用、冲突、结果表和清理 |

当前状态：S01–S12 已完成本地实现和模拟验证；S03 使用正式 Gerrit 已验证可用的整仓无历史
`git-upload-archive`。HTML 视觉与筛选导出不在本轮实现范围，正式数据源已固定为批次
JSON。

## 2. 实施顺序

```text
S01 → S02 → S03 → S04 → S05
                     ↓
              S06 → S07 → S08 → S09 → S10 → S11 → S12
```

## 3. 首版验收场景

1. 一个普通 Skill 完整下载、迁移、静态检查、AI 导入和结果落盘；
2. 两个不同 `skill_id`、同名同内容 Skill 共用 `content_id`，第二个产生 `RESULT_REUSED`；
3. 同名但内容不同的 Skill 重新执行检查；
4. 同一 `skill_id` 已存在不同内容时阻止覆盖；
5. 同仓库两个 Skill 只下载一次整仓归档，并只落盘两个白名单 Skill Root；
6. 每仓库完成后临时下载目录清空，永久 Skill、证据和批次结果仍存在；
7. 中断后不会重复覆盖已完成结果；
8. CSV 与 JSON 数量、Skill ID、结论、问题数和报告路径一致。
