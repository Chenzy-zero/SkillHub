# 下载、Static 与报告规则

## 1. 新批次标准流程

```text
冻结 CSV + Config
→ 按 repo_name + branch 分组
→ 冻结该组 branch HEAD，下载一次无历史整仓归档
→ 只提取清单登记的 Skill Root
→ 归档冻结 Skill Package，计算完整包 SHA-256
→ 判断满足 fingerprint 的结果复用
→ Cisco + SkillSpector Static Preparation
→ 当前仓库 Static 完成后立即清理 Runtime
→ 自动进入下一仓库，不等待该仓库 AI
→ 全 Batch Static 完成
→ 生成 INTERIM HTML/CSV/JSON
→ 生成一个 batch-wide AI Queue
→ completion-driven AI import / incremental report refresh
→ 全部 selected Skill resolved
→ 同一路径报告切换为 FINAL
```

新批次使用 `batch_wide_v2`。已经启动的 `repository_batch_v1` 批次按旧协议兼容续跑，不能静默换状态机。

## 2. 可信脚本职责

以下工作必须由确定性可信程序完成，不交给父 AI 临时判断：

- CSV 编码/字段/状态、配置冻结和 SHA-256；
- Git URL、SSH、Revision 冻结、无历史归档下载、路径提取和安全清理；
- 完整包 manifest/digest、content reuse fingerprint；
- Cisco/SkillSpector 执行、超时、健康检查、raw preservation、normalized result；
- `current-result.json` / `review-result.json`、Batch State、queue、dispatch lease；
- AI result Schema/expectation/digest/policy 校验与 `finalize_skill`；
- ledger、report exports、INTERIM/FINAL HTML；
- evidence index、恢复、幂等和清理门禁。

Git/scanner 必须以 argv 调用，不拼 Shell，不执行 Skill 内容。

## 3. Static、复用和仓库清理

- 两套 scanner 必须针对相同冻结 Skill digest。
- scanner 失败、报告缺失、coverage 不完整或规范化失败 => INCOMPLETE，不得进入 PASS。
- 每个仓库 Static 结果已经迁入 durable Skill/evidence 后，即可清理 `work/git_download` 的该仓 Runtime；不需要等待 AI。
- 同名同内容结果复用必须同时满足 package digest、scanner config/version、policy/review fingerprint 等要求。
- 复用仍为当前 `skill_id` 保存来源 Revision、durable result 和复用记录。
- 安全与质量分离；security gate 优先于质量分。

## 4. Report projection

Static 结束即可生成当前审计投影：

- `INTERIM`：展示 Static findings、AI=PENDING、Final=PENDING；不得把未完成 AI 显示为最终 PASS。
- completion-driven import 每成功一项立即刷新当前 HTML/CSV/JSON 投影。
- `FINAL`：只有全部 selected Skill 已 COMPLETED 或明确 INCOMPLETE 后形成。

唯一人工入口始终是：

```text
results/<batch-id>/skill-security-review-report.html
```

机器可读输出分两类：

- `skill-review-results.csv/json`：inventory ledger/reconciliation projection；
- `details.csv`、`batch-summary.json`、`failures.json`、`candidates.json`、`current-review-results.*`：标准化 report exports。

普通 HTML 不嵌入 raw scanner/AI evidence。Evidence Detail 使用 `evidence-index.json` 展示 type、relative path、size、SHA-256；RAW/SOURCE path-only，DERIVED/NORMALIZED 才允许受控相对导航。
