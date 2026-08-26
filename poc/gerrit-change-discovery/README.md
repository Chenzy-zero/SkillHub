# Gerrit Change Skill Discovery POC

这是 SkillHub 项目的日常 Gerrit 增量发现 POC。核心原则是：**不在每次提交时全仓扫描，而是先读取 Gerrit Code Review / Patchset 文件清单，只定位受影响 Skill，再对受影响 Skill Root 计算完整 Digest。**

## 当前完整流程

```text
Gerrit Code Review / Patchset
          ↓
Gerrit REST: Revision Files
          ↓
A / M / D / R / C
          ↓
新增 SKILL.md 或命中已有 Skill Root
          ↓
Affected Skills
          ↓
仅 fetch 当前 Patchset
          ↓
仅读取受影响 Skill Root 全部 tracked files
          ↓
SHA-256 Content Digest
          ↓
┌─────────────────────────────┐
│ JSON 原始证据              │
│ SQLite 结构化事实存档      │
│ HTML Dashboard 展示        │
└─────────────────────────────┘
```

历史全量盘点仍由 `../gerrit-skill-discovery/` 负责；本目录负责后续 Code Review 增量处理。

---

## 一键部署（Windows）

要求：

```text
Python 3.8+
Git CLI
PowerShell
可访问 Gerrit REST API
可通过 SSH clone/fetch Gerrit
```

更新仓库并进入目录：

```powershell
cd "D:\AI_Project\SKILL学习\SKILLHUB\SkillHub"
git pull
cd .\poc\gerrit-change-discovery
```

执行：

```powershell
.\deploy.ps1
```

部署脚本会自动：

1. 检查 Python / Git；
2. 如不存在则生成 `config.json`；
3. 创建 `data/`、`output/`、`workspace/`；
4. 初始化 SQLite；
5. 创建全部表和索引；
6. 生成初始 HTML Dashboard。

部署完成后目录包含：

```text
poc/gerrit-change-discovery/
├── config.json                 # 本地配置，不提交 Git
├── data/
│   └── skillhub-poc.db         # SQLite 事实库
├── output/
│   ├── dashboard/
│   │   └── index.html          # HTML Dashboard
│   ├── gerrit-change-discovery.log
│   └── change-*-patchset-*.json
└── workspace/                  # Git Patchset 本地缓存
```

---

## 配置

编辑 `config.json`：

```json
{
  "gerrit": {
    "base_url": "https://gerrit.company.com",
    "username": "1003304",
    "http_password": "你的 Gerrit HTTP Password",
    "http_password_env": "GERRIT_HTTP_PASSWORD",
    "verify_ssl": true,
    "ssh_username": "1003304",
    "ssh_url_template": "ssh://{username}@gerrit.company.com:29418/{project}"
  },
  "workspace": "./workspace",
  "output_dir": "./output",
  "database_path": "./data/skillhub-poc.db",
  "report_dir": "./output/dashboard",
  "inventory_file": "../gerrit-skill-discovery/output/skill_inventory.json",
  "calculate_digest": true,
  "auto_generate_report": true,
  "git_fetch_timeout_seconds": 120
}
```

POC 支持直接填写 `http_password`；如果不想在本地配置文件保存明文，也可以删除该字段值并使用：

```powershell
$env:GERRIT_HTTP_PASSWORD="你的 Gerrit HTTP Password"
```

`config.json`、`data/`、`workspace/` 和 `output/` 均已加入 `.gitignore`。

---

## 一键处理 Gerrit 单据

例如单据号 `123456`：

```powershell
.\run_change.ps1 -Change 123456 -VerboseLog
```

同时打开 Dashboard：

```powershell
.\run_change.ps1 -Change 123456 -VerboseLog -OpenDashboard
```

只测试 Gerrit 文件清单、不进行 Git Digest：

```powershell
.\run_change.ps1 -Change 123456 -NoDigest -VerboseLog
```

每次成功执行都会自动完成：

```text
Gerrit 分析
  ↓
JSON 留档
  ↓
SQLite 存档
  ↓
HTML Dashboard 刷新
```

---

## SQLite 数据模型

当前 POC 建立 5 张核心表：

### `skill_source`

一个来源 Skill：

```text
repository + skill_path + skill_name
```

保存 ACTIVE / INACTIVE / MOVED / DELETED 状态。

### `source_revision`

保存每次 Gerrit/Git 来源 Revision：

```text
Skill Source
  ↓
revision_sha
change_number
patchset
branch
  ↓
content_version_id
```

### `content_version`

按 `skill_digest` 去重。

因此可以表达：

```text
commit A -> Digest X
commit B -> Digest Y
commit C -> Digest Y
```

其中 B/C 是两个 Source Revision，但只对应一个 Content Version Y。

### `gerrit_patchset`

保存本次 Gerrit 单据、Patchset、Revision 和 Changed Files 原始信息。

### `change_skill_event`

保存：

```text
NEW_SKILL
UPDATED_SKILL
DELETED_SKILL
RENAMED_SKILL
COPIED_SKILL
```

以及 trigger file / reason 等证据。

数据库 Schema 位于：

```text
schema.sql
```

查看当前数据库统计：

```powershell
python .\database.py --config .\config.json --summary
```

---

## Inventory 策略

系统现在同时读取两个来源：

```text
Baseline JSON Inventory
        +
SQLite 中 ACTIVE Skill Source
        ↓
合并后的 Inventory
```

第一次上线可以通过旧的 Baseline POC 初始化历史资产；此后新发现的 Skill 会进入 SQLite，后续普通脚本修改就可以直接从数据库命中 Skill Root。

例如：

```text
M skills/jira-query/scripts/query.py
```

即使没有修改 `SKILL.md`，只要 SQLite / Baseline Inventory 中存在：

```text
skills/jira-query
```

就会识别为：

```text
UPDATED_SKILL
```

---

## Dashboard

默认地址：

```text
output/dashboard/index.html
```

当前展示：

- Skill Source 总数 / Active 数；
- Content Version 数；
- Source Revision 数；
- 已处理 Gerrit Patchset 数；
- Skill Event 数；
- Skill Source 列表；
- 最新 Revision / Digest；
- 最近 Gerrit 单据；
- 最近 Skill 变更事件。

也可单独重新生成：

```powershell
python .\report_generator.py --config .\config.json
```

HTML 不承担事实存储，数据源始终是 SQLite。

---

## JSON 为什么继续保留

SQLite 用于结构化查询；JSON 用于保存单次处理的完整原始证据。

默认：

```text
output/change-123456-patchset-3.json
```

所以当前采用：

```text
SQLite = 事实库
JSON   = 原始证据
HTML   = 展示层
```

后续迁移 PostgreSQL 时，Gerrit 分析逻辑、Digest 模型和展示模型可以继续复用。

---

## 已支持识别规则

| Gerrit 变化 | 结果 |
| --- | --- |
| 新增 `SKILL.md` | `NEW_SKILL` |
| 修改已有 `SKILL.md` | `UPDATED_SKILL` |
| 修改已登记 Skill Root 内其他文件 | `UPDATED_SKILL` |
| 删除 `SKILL.md` | `DELETED_SKILL` |
| Rename / Move | `RENAMED_SKILL` |
| Copy | `COPIED_SKILL` |
| `name` 变化 | 新 Source 候选，旧 Source 进入 INACTIVE |

Digest 对完整受影响 Skill Root 的 Git tracked 内容递归计算，不扫描其他无关目录。

---

## 当前目录

```text
poc/gerrit-change-discovery/
├── README.md
├── config.example.json
├── schema.sql
├── database.py
├── report_generator.py
├── deploy.ps1
├── run_change.ps1
├── gerrit_client.py
├── inventory.py
├── change_analyzer.py
├── skill_digest.py
├── main.py
└── .gitignore
```

## 下一阶段

当前 POC 已经具备：

```text
Gerrit Change
 -> Changed Files
 -> Skill Resolver
 -> Digest
 -> SQLite
 -> JSON
 -> Dashboard
```

下一阶段再建设：

```text
Gerrit patchset-created 自动事件
 -> Queue / 幂等任务
 -> Scanner Adapter
 -> Scan Result
 -> CM Review
 -> SkillHub Sync
```
