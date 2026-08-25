# Gerrit Skill Discovery POC v0.2

这个 POC 用来验证当前 Skill 安全管理策略最核心的几个假设：

1. 每个 Gerrit Patchset 都可以触发服务端检查；
2. `SKILL.md` 作为 Skill 识别锚点，所在目录作为 Skill Root；
3. Skill Root 下所有 Git tracked 内容构成 Skill Package；
4. `repository + skill_path + skill_name` 作为首版 Skill Source Key；
5. Commit SHA 作为 Source Revision，整个 Skill Package 的 SHA-256 作为 Content Version 身份；
6. 同一内容即使 Commit 不同，也可以通过相同 Digest 识别出来。

> Gerrit 不执行普通 Git 仓库里的标准 hooks。后续实时接入需要 Gerrit hooks plugin、`stream-events` 消费者或自研 Gerrit 插件。本 POC 的实时示例按 hooks plugin 的 `patchset-created` 参数实现。

## 文件

- `batch_scan.py`：读取配置，批量 clone/fetch 多个 SSH 仓库并逐一扫描。
- `scan_config.example.json`：批量扫描配置模板。
- `run_batch.ps1`：Windows PowerShell 一键运行入口。
- `skill_scan.py`：扫描单个本地 Git revision，支持普通仓库和 bare repository。
- `gerrit_hook.py`：接收 Gerrit hooks plugin 的 `patchset-created` 参数并调用扫描器。
- `patchset-created`：Gerrit hooks plugin 的示例 shell wrapper。

---

# 1. Windows 推荐用法：配置 SSH 地址后批量扫描

## 1.1 准备环境

需要：

- Python 3.8+
- Git CLI
- 当前 Windows 用户已经可以通过 SSH clone 对应 Gerrit 仓库

先验证：

```powershell
python --version
git --version
```

如果 Gerrit 使用 SSH Key，再单独确认一次 SSH/Git clone 权限。

## 1.2 生成本地配置

在当前目录执行：

```powershell
.\run_batch.ps1
```

如果 `scan_config.json` 不存在，脚本会自动从 `scan_config.example.json` 复制一份，并提示先填写配置。

也可以手工复制：

```powershell
Copy-Item .\scan_config.example.json .\scan_config.json
```

`scan_config.json` 已被 `.gitignore` 忽略，避免把公司内部 SSH 仓库地址误提交到 GitHub。

## 1.3 填写仓库 SSH 地址

示例：

```json
{
  "workspace": "./workspace",
  "output_dir": "./output",
  "refresh_existing": true,
  "continue_on_error": true,
  "include_manifest": false,
  "repositories": [
    {
      "enabled": true,
      "name": "Skill-CM",
      "url": "ssh://1003304@gerrit.company.com:29418/Skill-CM",
      "revision": "HEAD"
    },
    {
      "enabled": true,
      "name": "team-skill-repo",
      "url": "ssh://1003304@gerrit.company.com:29418/team/skill-repo",
      "revision": "HEAD"
    }
  ]
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `workspace` | clone 下来的本地仓库目录；相对路径以配置文件所在目录为基准 |
| `output_dir` | 报告和日志目录 |
| `refresh_existing` | 本地已 clone 时是否自动 `git fetch --all --prune` |
| `continue_on_error` | 某个仓库失败后是否继续扫描剩余仓库 |
| `include_manifest` | 最终 JSON 是否保留每个 Skill 的完整文件 Manifest |
| `enabled` | 是否扫描该仓库 |
| `name` | 数据库/报告中使用的逻辑仓库名 |
| `url` | Git/Gerrit SSH clone 地址 |
| `revision` | 扫描 revision；首版一般填写 `HEAD`，也可以填 branch/tag/commit |
| `local_dir` | 可选，指定 workspace 下的目录名 |

## 1.4 执行扫描

配置完成后再次执行：

```powershell
.\run_batch.ps1
```

也可以直接：

```powershell
python .\batch_scan.py --config .\scan_config.json
```

需要更多调试日志时：

```powershell
python .\batch_scan.py --config .\scan_config.json --verbose
```

## 1.5 执行过程

每个仓库会依次执行：

```text
读取配置
  -> 检查本地 workspace
  -> 不存在：git clone SSH仓库
  -> 已存在：git fetch --all --prune
  -> 解析 revision
  -> 递归扫描整个 Git tree
  -> 查找所有 SKILL.md
  -> 读取 frontmatter.name
  -> 确定 Skill Root
  -> 对 Root 下所有 tracked 内容计算 SHA-256 Digest
  -> 输出 Inventory 和日志
```

控制台会显示类似：

```text
2026-08-25 17:10:00 | INFO    | [1/3] 仓库: Skill-CM
2026-08-25 17:10:00 | INFO    | 开始 clone: ssh://user@gerrit.company.com:29418/Skill-CM
2026-08-25 17:10:04 | INFO    | 开始扫描: Skill-CM @ 8a73f0db75c1
2026-08-25 17:10:04 | INFO    | 扫描完成: 共发现 3 个 Skill
2026-08-25 17:10:04 | INFO    |   Skill: jira-query | path=skills/jira-query | digest=21c4... | files=4
```

## 1.6 输出文件

默认输出到：

```text
output/
├── batch_scan.log
├── skill_inventory.json
└── skill_inventory.jsonl
```

### `skill_inventory.json`

包含：

- 批次时间；
- 成功/失败仓库数；
- 每个仓库状态和错误；
- 所有发现的 Skill；
- repository；
- revision；
- skill_name；
- skill_path；
- source_key；
- SHA-256 digest；
- warning。

### `skill_inventory.jsonl`

每行一个 Skill，便于后续直接导数据库或做脚本处理。

### `batch_scan.log`

保留 clone/fetch/scan 过程日志以及 warning/error。

---

# 2. 当前 Skill 识别规则

## Skill Root

任何名为 `SKILL.md` 的 tracked file 所在目录都视为一个 Skill Root。

## Skill Name

优先读取 `SKILL.md` YAML frontmatter 顶层的 `name:`。POC 为保持零依赖，只实现简单的 `name:` 提取；未找到时退化为目录名并产生 warning。

## Skill Package

Skill Root 下**全部 Git tracked entries**都纳入 Digest，不忽略 README、references、assets、scripts 等内容。

因此：

- README 变化 -> Digest 变化；
- scripts 变化 -> Digest 变化；
- references/assets 变化 -> Digest 变化；
- 文件 mode 变化 -> Digest 变化。

“是否需要重新人工审核”不在 Digest 层判断，后续交给 Policy/Review 层。

## Source Key

```text
repository | skill_path | skill_name
```

Branch 当前不进入 Source Key；Branch/Change/Patchset/Commit 后续作为来源元数据保存。

## Content Digest

对 Skill Package 中 tracked entries 按相对路径排序，每项记录：

```text
git_mode + NUL + relative_path + NUL + sha256(raw_content) + LF
```

然后：

```text
skill_digest = SHA256(manifest)
```

Commit/Revision 和 Digest 分工：

- Commit/Revision：来源版本；
- Skill Digest：内容版本。

---

# 3. 特殊文件处理

POC 暂不直接阻断 symlink、submodule、Git LFS 或 binary，而是尽可能纳入摘要并产生 warning。

## symlink

- Hash Git 中保存的链接目标字符串；
- 不跟随链接；
- 输出 warning。

正式策略建议至少禁止指向 Skill Root 外部的 symlink。

## submodule

- 父仓库只能看到 gitlink commit id；
- POC 将该 commit id 纳入 Digest；
- 不拉取子仓库实际内容；
- 输出 warning。

若未来允许 submodule，需固定子仓库 commit 后递归拉取和扫描。

## Git LFS

- POC 识别 LFS pointer；
- 当前 Hash pointer，而非真实 LFS object；
- 输出 warning。

若未来允许 LFS，需获取真实 Object 后重新 Hash/扫描。

## Binary

- raw bytes 正常参与 SHA-256；
- 粗略识别 binary-like blob；
- 输出 warning；
- 不一刀切禁止。

后续应按图片/PDF/Office/可执行文件/压缩包等类型分类治理。

---

# 4. 单仓库手工扫描

如果已经有本地 Git 仓库：

```powershell
python .\skill_scan.py `
  --repo D:\code\Skill-CM `
  --revision HEAD `
  --repository-name Skill-CM `
  --no-manifest
```

`--repo` 必须是**本地 Git 仓库路径**，不是 SSH 地址。

---

# 5. Gerrit patchset-created POC

批量扫描用于 Baseline。真正做到“所有提交都自动触发”时，再接 Gerrit 服务端事件。

`gerrit_hook.py` 当前支持 hooks plugin `patchset-created` 风格参数，例如：

```bash
python3 gerrit_hook.py \
  --repo-base /var/gerrit/review_site/git \
  --output-file /tmp/skill-poc.jsonl \
  --project team/project \
  --branch feature/test \
  --change I0123456789 \
  --patchset 1 \
  --commit <commit-sha> \
  --uploader-username tester
```

---

# 6. 当前 POC 的刻意简化

v0.2 的批量 Baseline 仍是“扫描目标 Revision 中的所有 Skill”，尚未实现：

- changed files A/M/D/R/C；
- 只扫描受影响 Skill Root；
- Delete/Rename old revision 解析；
- 数据库；
- Canonical Skill 合并；
- 安全 Scanner；
- SkillHub API；
- 队列/重试。

下一阶段建议升级为：

```text
Patchset
 -> parent/current diff
 -> changed files A/M/D/R/C
 -> affected Skill Root
 -> Skill Source
 -> Source Revision
 -> Content Version
 -> DB
 -> Scanner
```
