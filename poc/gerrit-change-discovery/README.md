# Gerrit Change Skill Discovery POC

这个目录是日常增量治理的主 POC。它不再每次遍历整个仓库，而是围绕 **Gerrit Code Review / Patchset 的文件清单** 判断本次单据是否新增或影响 Skill。

## 目标流程

```text
Gerrit Code Review
        ↓
读取 current patchset
        ↓
GET revision files
        ↓
A / M / D / R / C
        ↓
┌────────────────────────────┐
│                            │
│  新增/修改 SKILL.md       │
│  或文件命中已有 Skill Root │
│                            │
└──────────────┬─────────────┘
               ↓
        Affected Skills
               ↓
仅 fetch 当前 Patchset Git ref
               ↓
仅计算受影响 Skill Root 完整 Digest
               ↓
输出 Change 分析结果
```

历史全量盘点仍由 `../gerrit-skill-discovery/` 目录负责。两者定位如下：

- `gerrit-skill-discovery`：一次性 Baseline / Inventory 初始化。
- `gerrit-change-discovery`：日常 Gerrit Code Review 增量识别。

## 已实现能力

- 按 Gerrit Change Number / Change-Id 查询当前 Patchset；
- 获取 Gerrit Revision Files 文件清单；
- 支持 `A/M/D/R/C`，Gerrit Modified 未显式返回 status 时按 `M` 处理；
- 新增 `SKILL.md` -> `NEW_SKILL`；
- 已有 `SKILL.md` 修改 -> `UPDATED_SKILL`；
- 删除 `SKILL.md` -> `DELETED_SKILL`；
- rename/move `SKILL.md` -> `RENAMED_SKILL`；
- copy `SKILL.md` -> `COPIED_SKILL`；
- 普通文件变更通过 Inventory 判断是否落在已有 Skill Root 内；
- `SKILL.md` name 变化时按当前策略创建新的 Skill Source 候选；
- 从 Gerrit RevisionInfo 获取 `refs/changes/...`，只 fetch 当前 Patchset；
- 只对受影响 Skill Root 获取完整 Git tracked 内容并计算 SHA-256 Digest；
- Windows PowerShell 一键执行；
- 详细控制台日志 + 文件日志；
- JSON 结果输出；
- 纯 Python 标准库 + Git CLI，无 pip 依赖。

## 目录

```text
poc/gerrit-change-discovery/
├── README.md
├── config.example.json
├── inventory.example.json
├── gerrit_client.py
├── inventory.py
├── change_analyzer.py
├── skill_digest.py
├── main.py
├── run_change.ps1
└── .gitignore
```

## 1. Windows 准备

要求：

```text
Python 3.8+
Git CLI
能够访问 Gerrit REST API
能够通过 SSH clone/fetch Gerrit 仓库
```

先更新项目：

```powershell
cd "D:\AI_Project\SKILL学习\SKILLHUB\SkillHub"
git pull
cd .\poc\gerrit-change-discovery
```

复制配置：

```powershell
Copy-Item .\config.example.json .\config.json
```

## 2. 配置 Gerrit

示例：

```json
{
  "gerrit": {
    "base_url": "https://gerrit.company.com",
    "username": "1003304",
    "http_password_env": "GERRIT_HTTP_PASSWORD",
    "verify_ssl": true,
    "ssh_username": "1003304",
    "ssh_url_template": "ssh://{username}@gerrit.company.com:29418/{project}"
  },
  "workspace": "./workspace",
  "output_dir": "./output",
  "inventory_file": "../gerrit-skill-discovery/output/skill_inventory.json",
  "calculate_digest": true,
  "git_fetch_timeout_seconds": 120
}
```

### REST 认证

POC 使用 Gerrit HTTP Basic Auth。不要把 HTTP Password 写进 `config.json`，建议只配置环境变量名：

```json
"http_password_env": "GERRIT_HTTP_PASSWORD"
```

PowerShell 当前窗口设置：

```powershell
$env:GERRIT_HTTP_PASSWORD="你的 Gerrit HTTP Password"
```

如果 Gerrit 允许匿名读取 Change，则可以不设置；如果需要登录，Gerrit 通常使用用户设置中生成的 HTTP Password，而不是 Windows/域账号密码。

### SSH 地址模板

项目名来自 Gerrit Change 的 `project` 字段。

例如项目：

```text
team/Skill-CM
```

模板：

```text
ssh://{username}@gerrit.company.com:29418/{project}
```

最终自动得到：

```text
ssh://1003304@gerrit.company.com:29418/team/Skill-CM
```

如果公司的 SSH 端口或地址不同，只修改模板即可。

## 3. Inventory

增量检测有两种判断来源：

### 新 Skill

Gerrit 文件清单直接出现：

```text
A tools/jira-query/SKILL.md
```

这种情况即使没有 Inventory，也可以发现。

### 已有 Skill 内普通文件修改

例如：

```text
M tools/jira-query/scripts/query.py
```

这时必须先知道：

```text
tools/jira-query
```

是已有 Skill Root，所以需要 Inventory。

推荐直接复用 Baseline POC 生成的：

```text
../gerrit-skill-discovery/output/skill_inventory.json
```

如果暂时没有 Baseline 数据，可以：

```powershell
Copy-Item .\inventory.example.json .\inventory.json
```

然后在 `config.json` 改为：

```json
"inventory_file": "./inventory.json"
```

Inventory 至少需要：

```json
{
  "skills": [
    {
      "repository": "team/Skill-CM",
      "skill_name": "jira-query",
      "skill_path": "tools/jira-query",
      "source_key": "team/Skill-CM|tools/jira-query|jira-query"
    }
  ]
}
```

## 4. 最简单的第一次测试：不算 Digest

建议先只验证 Gerrit REST + 文件清单识别，不进行 Git clone：

```powershell
python .\main.py --config .\config.json --change 123456 --no-digest --verbose
```

或者：

```powershell
.\run_change.ps1 -Change 123456 -NoDigest -VerboseLog
```

执行日志类似：

```text
[1/6] 获取 Gerrit Change 信息...
项目: team/Skill-CM
Patchset: 3
Revision: 5d8a...

[2/6] 获取本 Patchset 文件清单...
Changed Files: 4
A tools/jira-query/SKILL.md
A tools/jira-query/scripts/query.py
M README.md

[3/6] 加载 Skill Source Inventory...
[4/6] 基于文件清单识别受影响 Skill...
识别结果: NEW_SKILL ...
```

如果这一步正常，说明核心的“Gerrit Code Review -> Changed Files -> Skill 判断”已经跑通。

## 5. 开启 Digest

确认 REST 流程后，执行：

```powershell
python .\main.py --config .\config.json --change 123456 --verbose
```

程序会：

1. 从 Gerrit Change 取得 current revision SHA 和 `refs/changes/...`；
2. 根据项目名拼接 SSH URL；
3. 第一次自动 `git clone --no-checkout` 到 `workspace/`；
4. 后续复用本地仓库；
5. `git fetch origin refs/changes/...`；
6. 不扫描整个 repository，只对 `affected_skills` 的 Skill Root 执行 `git ls-tree`；
7. 对该 Root 内全部 Git tracked 内容计算 SHA-256 Digest。

## 6. 识别规则

### 新增

```text
A skills/demo/SKILL.md
```

结果：

```text
NEW_SKILL
```

### 已有 Skill 内脚本修改

Inventory：

```text
skills/demo
```

Change：

```text
M skills/demo/scripts/a.py
```

结果：

```text
UPDATED_SKILL
```

### SKILL.md 修改

```text
M skills/demo/SKILL.md
```

如果 `repository + path + name` 仍与 Inventory 一致：

```text
UPDATED_SKILL
```

如果 frontmatter `name` 改了，根据当前管理策略：

```text
NEW_SKILL
reason = SKILL.md name changed; create a new Skill Source by current policy
previous_sources = [...]
```

后续再由 Canonical Skill 流程决定是否关联。

### 删除

```text
D skills/demo/SKILL.md
```

结果：

```text
DELETED_SKILL
```

### Rename / Move

Gerrit 返回：

```text
status = R
old_path = skills/old/SKILL.md
path = skills/new/SKILL.md
```

结果：

```text
RENAMED_SKILL
old_skill_path = skills/old
skill_path = skills/new
```

### Copy

结果：

```text
COPIED_SKILL
```

## 7. 输出

默认目录：

```text
output/
├── gerrit-change-discovery.log
└── change-123456-patchset-3.json
```

JSON 示例：

```json
{
  "change": {
    "number": 123456,
    "project": "team/Skill-CM",
    "branch": "feature/demo",
    "patchset": 3,
    "revision": "5d8a...",
    "revision_ref": "refs/changes/56/123456/3"
  },
  "changed_file_count": 2,
  "affected_skill_count": 1,
  "affected_skills": [
    {
      "action": "UPDATED_SKILL",
      "repository": "team/Skill-CM",
      "skill_path": "tools/jira-query",
      "skill_name": "jira-query",
      "source_key": "team/Skill-CM|tools/jira-query|jira-query",
      "trigger_file": "tools/jira-query/scripts/query.py",
      "skill_digest": "...",
      "digest_algorithm": "SHA-256",
      "digest_status": "SUCCESS"
    }
  ]
}
```

## 8. 为什么仍然需要 Git fetch

**判断 Skill 是否受影响**只使用 Gerrit 文件清单，不扫描仓库。

但如果确认某个 Skill 受影响，需要生成完整 Content Version Digest，就必须拿到当前 Patchset 中该 Skill Root 的完整状态。例如单据只修改：

```text
skills/demo/scripts/a.py
```

Content Version 仍然应该基于：

```text
skills/demo/SKILL.md
skills/demo/README.md
skills/demo/scripts/a.py
skills/demo/scripts/b.py
skills/demo/references/...
```

因此流程是：

```text
Gerrit Files
  -> 定位 Affected Skill
  -> fetch Patchset
  -> 只读取 Affected Skill Root
  -> Digest
```

而不是重新全仓寻找所有 `SKILL.md`。

## 9. Gerrit REST API 依赖

POC 使用：

```text
GET /changes/{change-id}/detail?o=CURRENT_REVISION
GET /changes/{change-id}/revisions/{revision-id}/files/
GET /changes/{change-id}/revisions/{revision-id}/files/{file-id}/content
```

Gerrit JSON 响应的 XSSI 前缀 `)]}'` 已在客户端中自动去除。FileInfo 中 Modified 通常没有 `status` 字段，POC 按 `M` 处理；Rename/Copy 使用 `old_path`。

## 10. 当前仍是 POC 的部分

尚未做：

- 数据库存储；
- Gerrit `patchset-created` 自动事件接入；
- 队列与幂等任务；
- 自动 Scanner；
- CM Review；
- SkillHub API；
- 自动更新 Inventory；
- Canonical Skill 自动推荐关联；
- 对 merge commit 多 parent 的特殊处理。

下一阶段建议：

```text
patchset-created event
        ↓
main workflow
        ↓
Skill Source / Revision / Content Version DB
        ↓
Scan Queue
```

## 11. 推荐验证顺序

先选择一个真实 Gerrit 单据，按以下顺序测试：

```text
1. --no-digest
   验证 REST、Change、文件列表

2. 新增 SKILL.md 的单据
   验证 NEW_SKILL

3. 修改已有 Skill scripts 的单据
   验证 Inventory 匹配 UPDATED_SKILL

4. 开启 Digest
   验证 refs/changes fetch + Skill Root SHA-256

5. Rename/Delete 单据
   验证 Source 生命周期事件
```

这样可以很快确认整个增量方案是否适合公司现有 Gerrit 数据。
