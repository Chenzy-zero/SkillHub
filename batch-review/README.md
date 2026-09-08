# Skill 批量安全审查

`batch-review/` 是一个可独立复制和执行的 Skill 安全审查项目。它负责：冻结 Git 来源、提取完整 Skill Package、运行 Cisco AI Skill Scanner + NVIDIA SkillSpector、为每个 Skill 生成隔离 AI Reviewer 任务、合并安全/质量结论，并持续生成可审计报告。

当前标准流程已经从“逐仓库等待 AI”升级为：

```text
CSV + Config 冻结
→ Repo A Static Preparation → 清理 Repo A Runtime
→ Repo B Static Preparation → 清理 Repo B Runtime
→ ...全部仓库 Static 完成
→ INTERIM HTML/CSV/JSON
→ 一个 Batch-wide AI Queue
→ rolling_largest_first + ai_reviews 并发
→ Reviewer 任意一个完成
→ 单项校验 / finalize / 报告刷新
→ 立即补派一个新 Reviewer
→ 全部 resolved
→ FINAL 报告
```

安全边界、目录职责和维护规则见 [`AGENTS.md`](AGENTS.md)。canonical 业务规则位于 `.agents/rules/`；canonical AI 审查策略和 Schema 位于 `.agents/skills/skill-security-review/`。`.claude/` 和 `.codex/` 是客户端适配/隔离 Agent 定义，不是第二份业务策略。

---

## 1. 最快开始

### Windows

```text
第一次：init.cmd
之后：review.cmd
```

也可以在 AI 客户端中直接使用：

```text
Codex CLI:   $auto-skill-review
Claude Code: /auto-skill-review
```

只查看状态：

```text
Codex CLI:   $ask-cc
Claude Code: /ask-cc
```

### Linux / CentOS / macOS

```bash
./init.sh
./review.sh
```

开发环境：

```bash
python -m pip install -e '.[dev]'
python -m pytest tests -q
```

支持 Python 3.11–3.14。扫描器真实安装仍按批准的内网依赖源和固定版本执行；自动审查不会静默安装扫描器、修改真实配置或替代首次初始化确认。

---

## 2. 目录职责：什么能删，什么不能删

profile 初始化默认使用 `.batch-review/<profile>/`。生产配置可以把每个 root 放到独立受控磁盘，但职责不变。第一阶段为了历史批次兼容，不强制改名或迁移既有路径。

| 物理目录 / 配置键 | 分类 | 内容 | 生命周期 |
|---|---|---|---|
| `work` / `workspace.root` | **Runtime** | scanner work、临时过程文件 | 没有活动/恢复任务后可由可信脚本清理 |
| `git_download` | **Runtime** | 无历史仓库归档与本地提取临时区 | 每仓 Static 完成即清理；失败恢复前不要手工删除 |
| `manifests` / `manifest_root` | **State** | Batch State、task index、AI queue、dispatch lease | 活动批次不可删除；`manifest_root` 是历史键名，实际职责是 state/control plane |
| `skills` / `skills_root` | **Archive** | 冻结 Skill Package、`current-result.json`、`review-result.json` | 持久审计与结果复用基础，不是缓存 |
| `restricted-evidence` / `evidence_root` | **Evidence** | scanner raw/normalized、AI imported result、final result、`evidence-index.json` | 受限持久审计数据；不要当 Runtime 清理 |
| `private-candidates` / `candidate_root` | **Candidate staging** | 通过门禁后的私密候选包 | 发布中间区，不替代审计 Archive/Evidence；按发布治理策略处置 |
| `results/<batch-id>` / `results_root` | **Reports** | HTML 工作台、标准化 CSV/JSON、ledger projection | 完成批次的人工/系统消费输出 |

### 唯一人工报告入口

```text
results/<batch-id>/skill-security-review-report.html
```

操作人员查看批次时应优先打开这个 HTML。不要把某个 CSV 当成第二套人工主报告。

---

## 3. Ledger 与 Report Export 的区别

`results/<batch-id>/` 中有两类机器可读结果，职责不同：

### Ledger / reconciliation projection

```text
skill-review-results.csv
skill-review-results.json
```

它们按原始 inventory 行重建，保留上游字段并追加审查字段，主要用于：

- 与原 Skill 台账/发布系统对账；
- 后续数据库导入或自动化消费；
- 确认每个 `source_row_id` / `skill_id` 是否已有 durable result。

它们不是标准化审计明细视图。

### Standard report exports

```text
batch-summary.json
details.csv
failures.json
candidates.json
current-review-results.csv
current-review-results.json
skill-security-review-report.html
```

这些文件服务于审计汇总、筛选和报告投影。`current-review-results.*` 可在 INTERIM 阶段出现；HTML 同一路径会从 INTERIM 更新为 FINAL。

---

## 4. 当前状态机

### Static 阶段

新 Batch 使用 `batch_wide_v2`：

1. 根据 CSV 中 selected Skill 按 `repo_name + branch` 分组。
2. 每组冻结一次 branch HEAD，下载一次无历史整仓归档。
3. 只提取清单登记 Skill Root，不 checkout、不执行 Hook、不运行 Skill 内容。
4. 每个 Skill 生成完整包 SHA-256。
5. Cisco 与 SkillSpector 对同一冻结内容运行本地静态扫描。
6. 每个仓库 Static 完成后立即清理仓库 Runtime，不等待该仓库 AI。
7. 所有仓库 Static 完成后先生成 INTERIM 报告，再暴露一个 Batch-wide AI Queue。

### AI 阶段

Batch Queue 每项只提供调度所需的最小字段：

```text
task_id
handoff
expected_result
review_size_bytes
```

父协调 Agent 不读取 Skill 内容、scanner evidence、manifest 或其他 Skill。每个 Skill 启动一个全新 Reviewer；默认最多 `[concurrency].ai_reviews` 个并发，采用 `rolling_largest_first`。

completion-driven 导入规则：

1. Reviewer 只写自己的 `expected_result`。
2. 父协调层回传 `task_id + dispatch_session` completion event。
3. 可信脚本只 import 这一项：Schema/expectation/digest/policy 校验 → `finalize_skill`。
4. 成功后立即更新 durable result 和当前 HTML/CSV/JSON 投影。
5. 释放一个 dispatch lease，再补派一个待审 Task。
6. malformed/missing result 只标记对应 Task 失败，不回滚其他已完成 Skill，也不让父 Agent代审。

旧的 `repository_batch_v1` 已启动批次仍保留兼容续跑路径；新批次不会静默复用旧协议。

---

## 5. Evidence 与报告导航

每个 evidence task 目录维护原子化：

```text
evidence-index.json
```

索引保存：

```text
label / type / relative path / size / SHA-256 / mtime / navigation
```

新 evidence 在可信 `EvidenceStore` 写入/复制时已经计算 SHA-256，因此索引复用这个摘要，不为报告再 hash 一次。旧批次第一次升级生成报告时会安全 bootstrap 一次 index；后续增量刷新不重复 hash 未变化历史 evidence。

导航策略：

- `RAW`：只显示相对路径、大小、SHA-256；**不生成打开链接**。
- `SOURCE`：同样 path-only。
- `NORMALIZED` / `DERIVED`：再次通过 evidence-root、symlink、regular-file 校验后，可生成相对文件链接。
- 删除、缺失、越界、symlink 或 metadata 不一致的 evidence 不产生导航。
- HTML 不嵌入 scanner raw report 或 imported AI raw result。

---

## 6. 配置

三个模板：

```text
config/review.example.toml
config/review.company.example.toml
config/review.github.example.toml
```

canonical AI policy 配置：

```toml
[ai]
skill_path = "../.agents/skills/skill-security-review"
result_schema_path = "../.agents/skills/skill-security-review/references/review-result.schema.json"
```

历史本机 TOML 如果仍指向已移除的：

```text
skills/skill-security-review
```

加载器只对这一组**已知仓库旧路径**做内存重定向到 `.agents/skills/skill-security-review`；不会改写用户配置，也不会重定向用户自定义的外部 policy 路径。

正式执行前必须确认：

- `batch.inventory_csv`
- `batch.included_statuses`
- `gerrit.*` 只读连接/仓库范围
- Cisco / SkillSpector 批准版本与 executable
- workspace 各 root 的容量、权限、留存策略

台账支持 UTF-8 / UTF-8 BOM / BOM UTF-16 / GBK/GB18030，并保存原始 CSV SHA-256 与识别编码，不改写源文件。

---

## 7. Canonical 路由

| 内容 | 路径 |
|---|---|
| 项目边界与维护入口 | `AGENTS.md` |
| 工作区/输入规则 | `.agents/rules/01-workspace-and-input-rules.md` |
| Static/报告规则 | `.agents/rules/02-review-workflow-rules.md` |
| AI Queue/completion/恢复规则 | `.agents/rules/03-ai-state-and-recovery-rules.md` |
| 安全审查策略 | `.agents/skills/skill-security-review/SKILL.md` |
| 结果 Schema | `.agents/skills/skill-security-review/references/review-result.schema.json` |
| Codex 自动调度 Skill | `.agents/skills/auto-skill-review/SKILL.md` |
| Claude 自动调度 Skill | `.claude/skills/auto-skill-review/SKILL.md` |
| Codex Reviewer Agent | `.codex/agents/` |
| 配置 | `config/` |
| 台账 | `inventory/` |
| 实现 | `src/`、`tools/` |
| 测试 | `tests/` |

根目录当前没有 `docs/`、`rules/`、`skills/`。不要在核心路由中重新引用这些不存在目录。

---

## 8. 安装扫描器

扫描器必须使用公司批准的离线/代理依赖源并固定版本；当前模板为：

- Cisco AI Skill Scanner `2.0.13`
- NVIDIA SkillSpector `2.5.1`

执行命令必须保持本地静态模式，尤其 SkillSpector 的 `--no-llm` 不得移除。安装/健康检查由 `tools/install_scanners.py` 和 operator 入口负责；自动模式不会在未授权情况下静默安装软件。

---

## 9. 回归与 CI

本地完整回归：

```bash
python -m pytest tests -q
```

GitHub CI：

```text
.github/workflows/batch-review-regression.yml
```

CI 使用 Linux/Windows Python 矩阵，运行整个 `batch-review/tests`，覆盖：

- Static/current-result 阶段状态；
- INTERIM/FINAL partial report；
- cross-repository Batch-wide AI Queue；
- completion-driven 单项 AI import 与 dispatch lease；
- evidence-index / legacy bootstrap / 安全导航；
- 中断恢复；
- `repository_batch_v1` 与旧 policy path 兼容；
- profile 初始化与核心文档路由。

CI 不需要真实 Gerrit、真实 scanner 或真实模型凭据；这些边界在单元/集成回归中使用受控 fixture/mock。真实内网 scanner/Gerrit 联调仍属于部署验收，不应把生产凭据放进 GitHub Actions。
