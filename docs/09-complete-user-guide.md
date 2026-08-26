# SkillHub 安全管理项目完整使用说明

> 本文面向项目维护者、CM、Gerrit 管理员及后续安全治理人员，说明当前 SkillHub 安全管理项目从部署、配置、Baseline 初始化、Gerrit Submit 自动触发、MySQL 存档到日常排障的完整使用方式。

---

## 1. 项目目标

本项目用于治理公司 Gerrit 仓库中的 Agent Skill / `SKILL.md` 资产。

当前第一阶段重点解决以下问题：

1. 自动识别 Gerrit 仓库中的 Skill；
2. 以 `SKILL.md` 所在目录作为 Skill Root；
3. 对整个 Skill Package 生成稳定的 SHA-256 Digest；
4. 保存 Skill Source、Git Revision、Content Version、Gerrit Patchset 等历史数据；
5. 在 Gerrit Submit 时自动触发 Skill 发现/版本计算/数据库存档流程；
6. 为后续接入安全 Scanner、CM Review 和 iflytek SkillHub 提供统一数据基础。

当前项目遵循的核心版本原则：

> **Commit / Revision 是来源版本，Digest 是内容版本；安全扫描和审核绑定内容版本，Git 追溯绑定来源版本。**

---

## 2. 当前总体架构

```text
开发人员
   │
   │ git push HEAD:refs/for/<branch>
   ▼
Gerrit Code Review
   │
   │ Review 完成
   ▼
用户点击 Submit
   │
   ▼
Gerrit Hooks Plugin
   │
   │ submit hook
   ▼
gerrit-change-discovery/main.py
   │
   ├── Gerrit REST：读取 Change / Patchset / Changed Files
   ├── Baseline + Database Inventory：定位受影响 Skill
   ├── Git：仅获取当前 Patchset
   ├── SHA-256：计算受影响 Skill Root Digest
   ├── JSON：保存原始分析证据
   ├── MySQL：保存结构化事实数据
   └── HTML Dashboard：展示资产与历史
   │
   ▼
exit 0 / exit != 0
   │
   ├── 0      -> Gerrit 继续 Submit
   └── 非 0   -> Gerrit 拒绝 Submit
```

当前完整流程部署后，**日常不需要全仓扫描**。日常处理以 Gerrit Changed Files 为入口，仅对命中的 Skill Root 获取完整内容并计算 Digest。

历史已有 Skill 则通过 Baseline 初始化一次。

---

## 3. 仓库主要目录

```text
SkillHub/
├── README.md
├── AGENTS.md
├── docs/
│   ├── 01-open-source-skillhub-evaluation.md
│   ├── 02-skill-security-management-strategy.md
│   ├── 03-gerrit-skill-discovery-and-review-design.md
│   ├── 04-requirements.md
│   ├── 05-task-breakdown.md
│   ├── 06-data-model.md
│   ├── 07-rollout-plan.md
│   ├── 08-current-flow-reproduction.md
│   └── 09-complete-user-guide.md
│
└── poc/
    ├── gerrit-skill-discovery/          # 历史 Baseline / Inventory 初始化
    └── gerrit-change-discovery/         # 日常增量识别 + DB + Dashboard + Submit Hook
```

### 3.1 `gerrit-skill-discovery`

用于第一次对历史 Gerrit 仓库进行全量盘点，输出 `skill_inventory.json`。

它的定位是：

```text
历史数据初始化 / Baseline
```

不是日常运行入口。

### 3.2 `gerrit-change-discovery`

当前日常主流程。

主要文件：

```text
poc/gerrit-change-discovery/
├── main.py                     # 主流程
├── gerrit_client.py            # Gerrit REST
├── change_analyzer.py          # Changed Files -> Affected Skills
├── inventory.py                # Baseline + Database Inventory
├── skill_digest.py             # Skill Package SHA-256
├── database.py                 # MySQL / SQLite 数据访问
├── report_generator.py         # HTML Dashboard
│
├── config.example.json         # 配置模板
├── requirements.txt            # Python 依赖
├── schema.mysql.sql            # MySQL 5 张核心表
├── create_database.mysql.sql   # MySQL 建库辅助 SQL
├── schema.sql                  # SQLite Schema
│
├── deploy.ps1                  # Windows 一键部署
├── deploy.bat
├── run_change.ps1              # 手工执行某个 Gerrit Change
│
└── gerrit-hooks/
    ├── submit                   # Gerrit Submit Hook
    ├── install.sh               # Hook 安装脚本
    └── README.md
```

---

## 4. 环境要求

### 4.1 Windows POC / 调试环境

建议：

```text
Windows 10 / 11
Python 3.8+
Git CLI
PowerShell
可访问 Gerrit HTTPS REST
可通过 SSH clone/fetch Gerrit 仓库
可访问 MySQL
```

检查：

```powershell
python --version
git --version
```

### 4.2 Gerrit Server 环境

Submit Hook 部署在 Gerrit Server 上时，需要：

```text
Linux
Python 3
Git CLI
Gerrit Hooks Plugin
Gerrit 服务账号可以访问 Gerrit REST
Gerrit 服务账号可以访问 Gerrit Git SSH
Gerrit 服务账号可以访问 MySQL
```

Gerrit 服务账号还需要对项目运行目录拥有：

```text
读取 config.json
读取 Python 代码
写 workspace/
写 output/
写 output/hooks/
```

---

# 第一部分：MySQL 部署

## 5. 创建 MySQL Database

推荐在现有 MySQL Server 中单独建立一个数据库：

```text
skillhub_security
```

执行：

```sql
CREATE DATABASE IF NOT EXISTS skillhub_security
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
```

仓库也提供：

```text
poc/gerrit-change-discovery/create_database.mysql.sql
```

如果公司由 DBA 统一建库，则只需要让 DBA 创建 Database，后续表结构可以：

- POC 阶段由程序自动创建；
- 正式环境由 DBA 执行 `schema.mysql.sql`。

---

## 6. 当前 MySQL 表

当前建立 5 张核心表：

```text
skill_source
source_revision
content_version
gerrit_patchset
change_skill_event
```

关系：

```text
gerrit_patchset
        │
        ▼
change_skill_event
        │
        ▼
skill_source
        │
        ▼
source_revision
        │
        ▼
content_version
```

### 6.1 `skill_source`

表示一个 Skill 来源。

当前 Source Key：

```text
repository + skill_path + skill_name
```

例如：

```text
repository = AI/skills
skill_path = tools/jira-query
skill_name = jira-query

source_key = AI/skills|tools/jira-query|jira-query
```

### 6.2 `source_revision`

记录 Skill Source 的每个 Git Revision。

例如：

```text
jira-query
├── commit A
├── commit B
└── commit C
```

每个 commit 都保留。

### 6.3 `content_version`

使用完整 Skill Package 的 SHA-256 Digest 作为内容版本。

例如：

```text
commit A -> digest X
commit B -> digest Y
commit C -> digest Y
```

数据库中：

```text
Source Revision = 3 条
Content Version = 2 条
```

### 6.4 `gerrit_patchset`

保存：

```text
repository
change_number
patchset
revision_sha
branch
subject
status
changed_files_json
raw_result_json
observed_at
```

### 6.5 `change_skill_event`

保存 Gerrit Patchset 对 Skill 产生的事件：

```text
NEW_SKILL
UPDATED_SKILL
DELETED_SKILL
RENAMED_SKILL
COPIED_SKILL
```

---

## 7. MySQL 权限

POC 自动建表时，账号建议具备：

```text
SELECT
INSERT
UPDATE
DELETE
CREATE
ALTER
INDEX
```

正式环境建议：

```text
应用账号：SELECT / INSERT / UPDATE / DELETE
DDL：由 DBA 管理
```

不要给应用账号不必要的：

```text
SUPER
DROP DATABASE
CREATE USER
GRANT OPTION
```

---

# 第二部分：项目配置

## 8. 创建 `config.json`

进入：

```text
poc/gerrit-change-discovery
```

执行：

```powershell
Copy-Item .\config.example.json .\config.json
```

`config.json` 已被 `.gitignore` 忽略，不应提交到代码仓库。

完整示例：

```json
{
  "gerrit": {
    "base_url": "https://gerrit.company.com",
    "username": "your_username",
    "http_password": "your_gerrit_http_password",
    "http_password_env": "GERRIT_HTTP_PASSWORD",
    "verify_ssl": true,
    "ssh_username": "your_username",
    "ssh_url_template": "ssh://{username}@gerrit.company.com:29418/{project}"
  },
  "database": {
    "type": "mysql",
    "host": "127.0.0.1",
    "port": 3306,
    "database": "skillhub_security",
    "username": "skillhub_app",
    "password": "",
    "password_env": "SKILLHUB_DB_PASSWORD",
    "charset": "utf8mb4",
    "connect_timeout_seconds": 10,
    "ssl": false,
    "ssl_ca": "",
    "ssl_cert": "",
    "ssl_key": ""
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

---

## 9. Gerrit 配置字段

### `base_url`

填写 Gerrit 根地址：

```text
https://gerrit.company.com
```

不要主动写：

```text
/a
```

程序在认证请求时会自动处理 `/a`。

### `username`

填写 Gerrit 用户名。

不要默认使用邮箱；应使用 Gerrit HTTP Credentials 对应的账号名。

### `http_password`

POC 可以直接填写 Gerrit HTTP Password：

```json
"http_password": "xxxx"
```

它通常是：

```text
Gerrit -> Settings -> HTTP Credentials
```

生成的 HTTP Password，而不一定是 Windows / LDAP 登录密码。

正式环境建议保持为空：

```json
"http_password": ""
```

然后设置：

```bash
export GERRIT_HTTP_PASSWORD='xxxxx'
```

Windows：

```powershell
$env:GERRIT_HTTP_PASSWORD="xxxxx"
```

### `verify_ssl`

正常应：

```json
true
```

如果 POC 环境使用内部自签证书，可临时：

```json
false
```

正式环境建议导入公司 CA，而不是长期关闭证书校验。

### `ssh_url_template`

例如：

```text
ssh://{username}@gerrit.company.com:29418/{project}
```

程序会将 Gerrit Change 返回的 `project` 自动代入。

---

## 10. MySQL 配置字段

最重要的字段：

```text
host
port
database
username
password
```

例如：

```json
"database": {
  "type": "mysql",
  "host": "192.168.100.20",
  "port": 3306,
  "database": "skillhub_security",
  "username": "skillhub_app",
  "password": "xxxx",
  "password_env": "SKILLHUB_DB_PASSWORD",
  "charset": "utf8mb4",
  "connect_timeout_seconds": 10,
  "ssl": false,
  "ssl_ca": "",
  "ssl_cert": "",
  "ssl_key": ""
}
```

生产环境建议：

```json
"password": ""
```

通过环境变量：

```bash
export SKILLHUB_DB_PASSWORD='xxxxx'
```

Windows：

```powershell
$env:SKILLHUB_DB_PASSWORD="xxxxx"
```

当：

```json
"type": "mysql"
```

时，`database_path` 不参与运行。

---

# 第三部分：一键部署与验证

## 11. Windows 一键部署

更新代码：

```powershell
git pull
cd .\poc\gerrit-change-discovery
```

配置好 `config.json` 后执行：

```powershell
.\deploy.ps1
```

或者双击：

```text
deploy.bat
```

部署流程：

```text
检查 Python / Git
    ↓
安装 requirements.txt
    ↓
校验 Python 代码
    ↓
读取 config.json
    ↓
创建 workspace/output/data
    ↓
检查 MySQL
    ↓
初始化 Schema
    ↓
读取统计
    ↓
生成 Dashboard
```

成功时应看到类似：

```text
Database connection OK: mysql://skillhub_app@192.168.100.20:3306/skillhub_security
Database schema initialized: mysql://skillhub_app@192.168.100.20:3306/skillhub_security
```

---

## 12. 单独验证数据库

```powershell
python .\database.py `
  --config .\config.json `
  --check `
  --init `
  --summary
```

第一次数据库为空时，正常结果类似：

```json
{
  "skill_sources": 0,
  "active_sources": 0,
  "content_versions": 0,
  "source_revisions": 0,
  "patchsets": 0,
  "skill_events": 0
}
```

---

# 第四部分：Baseline 初始化

## 13. 为什么需要 Baseline

假设已有 Skill：

```text
skills/demo/SKILL.md
skills/demo/scripts/a.py
```

未来某个 Gerrit Change 只修改：

```text
M skills/demo/scripts/a.py
```

Changed Files 中没有 `SKILL.md`。

系统只有提前知道：

```text
skills/demo
```

是一个已有 Skill Root，才能识别此次修改影响 Skill。

因此第一次上线前建议做一次 Baseline。

---

## 14. 配置 Baseline

进入：

```powershell
cd .\poc\gerrit-skill-discovery
```

执行：

```powershell
.\run_batch.ps1
```

第一次会生成：

```text
scan_config.json
```

配置仓库：

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
      "name": "team/Skill-CM",
      "url": "ssh://user@gerrit.company.com:29418/team/Skill-CM",
      "revision": "HEAD"
    }
  ]
}
```

再次执行：

```powershell
.\run_batch.ps1
```

默认生成：

```text
output/skill_inventory.json
output/skill_inventory.jsonl
output/batch_scan.log
```

日常 POC 的配置默认读取：

```json
"inventory_file": "../gerrit-skill-discovery/output/skill_inventory.json"
```

---

# 第五部分：手工验证 Gerrit Change

## 15. 先使用 `--no-digest`

在真正接 Submit Hook 前，建议先人工验证一张 Gerrit 单据。

```powershell
cd .\poc\gerrit-change-discovery

.\run_change.ps1 `
  -Change 123456 `
  -NoDigest `
  -VerboseLog
```

此步骤主要验证：

```text
Gerrit REST
Change
Patchset
Changed Files
Skill 识别
MySQL
```

不验证 Git SSH Digest。

---

## 16. 开启 Digest

REST 流程正常后：

```powershell
.\run_change.ps1 `
  -Change 123456 `
  -VerboseLog
```

执行：

```text
读取 Gerrit Change
      ↓
读取 current revision
      ↓
获取 Revision Files
      ↓
识别 Affected Skill
      ↓
fetch refs/changes/... 当前 Patchset
      ↓
只读取 Affected Skill Root
      ↓
生成 SHA-256
      ↓
写 JSON
      ↓
写 MySQL
      ↓
刷新 Dashboard
```

---

## 17. 建议验证的 Gerrit 场景

至少验证：

| 场景 | Changed Files | 预期 |
| --- | --- | --- |
| 新增 Skill | `A skills/demo/SKILL.md` | `NEW_SKILL` |
| 修改 SKILL.md | `M skills/demo/SKILL.md` | `UPDATED_SKILL` |
| 修改脚本 | `M skills/demo/scripts/a.py` | `UPDATED_SKILL` |
| 删除 Skill | `D skills/demo/SKILL.md` | `DELETED_SKILL` |
| Rename | `R old/SKILL.md -> new/SKILL.md` | `RENAMED_SKILL` |
| Copy | `C old/SKILL.md -> new/SKILL.md` | `COPIED_SKILL` |

---

# 第六部分：Gerrit Submit 自动触发

## 18. 当前触发策略

当前使用策略：

> **只有用户点击 Gerrit Submit 时，才触发本项目的完整分析流程。**

即：

```text
push refs/for/*
    ↓
不执行本项目扫描
    ↓
Code Review
    ↓
Submit
    ↓
执行扫描
```

这符合当前 POC 的目标：验证“Submit -> Skill Discovery -> Digest -> DB”的完整链路。

---

## 19. Gerrit Hooks Plugin

Gerrit 不会执行普通 Git Repository Hooks。

需要安装 Gerrit Hooks Plugin，并启用同步 `submit` Hook。

在：

```text
$GERRIT_SITE/etc/gerrit.config
```

配置：

```ini
[hooks]
    path = hooks
    submitHook = submit
    syncHookTimeout = 180
```

说明：

```text
path = hooks
```

对应：

```text
$GERRIT_SITE/hooks/
```

`syncHookTimeout = 180` 是当前 POC 为完整扫描预留的同步超时，后续应根据真实耗时调整。

修改后根据公司 Gerrit 运维方式 Reload Hooks Plugin 或重启相关 Gerrit 服务。

---

## 20. 在 Gerrit Server 部署程序

例如：

```text
/opt/skillhub/gerrit-change-discovery
```

确保目录中存在：

```text
main.py
config.json
database.py
gerrit_client.py
inventory.py
change_analyzer.py
skill_digest.py
report_generator.py
requirements.txt
gerrit-hooks/
```

安装依赖：

```bash
cd /opt/skillhub/gerrit-change-discovery
python3 -m pip install -r requirements.txt
```

然后验证：

```bash
python3 database.py --config config.json --check --init --summary
```

---

## 21. 安装 Submit Hook

假设：

```text
GERRIT_SITE=/var/gerrit/review_site
POC_HOME=/opt/skillhub/gerrit-change-discovery
```

执行：

```bash
cd /opt/skillhub/gerrit-change-discovery/gerrit-hooks

chmod +x submit install.sh

./install.sh \
  /var/gerrit/review_site \
  /opt/skillhub/gerrit-change-discovery
```

最终：

```text
/var/gerrit/review_site/hooks/submit
```

检查：

```bash
ls -l /var/gerrit/review_site/hooks/submit
```

必须具备执行权限：

```text
-rwxr-xr-x
```

同时确认 Gerrit 服务用户能够执行该文件。

---

## 22. Submit Hook 接收到的参数

Gerrit Hooks Plugin 调用类似：

```text
submit \
  --change Ixxxxxxxxxxxxxxxx \
  --project team/skills \
  --branch develop \
  --submitter "User Name" \
  --submitter-username user001 \
  --patchset 3 \
  --commit abcdef123456...
```

Hook 会调用：

```bash
python3 main.py \
  --config config.json \
  --change Ixxxxxxxxxxxxxxxx \
  --expected-revision abcdef123456... \
  --expected-patchset 3
```

`--expected-revision` 和 `--expected-patchset` 用来确认：

> 当前 REST 返回的 Revision 必须与用户此刻真正 Submit 的 Patchset 完全一致。

如果不一致，流程直接失败，不会误扫其它 Patchset。

---

## 23. 手工模拟 Submit Hook

真正点击 Submit 前，建议先手工模拟：

```bash
/var/gerrit/review_site/hooks/submit \
  --change Ixxxxxxxxxxxxxxxx \
  --project team/skills \
  --branch develop \
  --submitter test \
  --submitter-username test \
  --patchset 3 \
  --commit abcdef1234567890abcdef1234567890abcdef12
```

查看：

```bash
echo $?
```

返回：

```text
0
```

表示允许 Submit。

非 0：

```text
Gerrit 将拒绝 Submit
```

---

## 24. Hook 路径覆盖

默认：

```text
POC_HOME=/opt/skillhub/gerrit-change-discovery
PYTHON=/usr/bin/python3
CONFIG=/opt/skillhub/gerrit-change-discovery/config.json
LOG_DIR=/opt/skillhub/gerrit-change-discovery/output/hooks
```

可以通过 Gerrit 服务进程环境变量覆盖：

```text
SKILLHUB_POC_HOME
SKILLHUB_PYTHON
SKILLHUB_CONFIG
SKILLHUB_HOOK_LOG_DIR
```

例如：

```bash
export SKILLHUB_POC_HOME=/data/skillhub/gerrit-change-discovery
export SKILLHUB_PYTHON=/usr/local/bin/python3
export SKILLHUB_CONFIG=/etc/skillhub/config.json
export SKILLHUB_HOOK_LOG_DIR=/var/log/skillhub
```

---

# 第七部分：结果与日志

## 25. JSON 原始证据

默认：

```text
output/change-<change>-patchset-<n>.json
```

例如：

```text
output/change-123456-patchset-3.json
```

保存：

```text
Change 信息
Patchset
Revision
Branch
Changed Files
Affected Skills
Digest
Manifest
Warnings
```

JSON 应视为：

> 单次 Gerrit 分析的原始证据快照。

---

## 26. 主程序日志

默认：

```text
output/gerrit-change-discovery.log
```

用于排查：

```text
REST
Inventory
Skill Resolver
Git fetch
Digest
Database
Dashboard
```

---

## 27. Submit Hook 日志

默认：

```text
output/hooks/
```

文件示例：

```text
submit-team_skills-Iabcdef-3.log
```

其中包含：

```text
event
change
project
branch
patchset
commit
submitter
main.py 输出
```

如果流程失败，Hook 会返回非 0，并向 Gerrit 用户输出部分失败日志。

---

## 28. HTML Dashboard

默认：

```text
output/dashboard/index.html
```

手工打开：

```powershell
Start-Process .\output\dashboard\index.html
```

或者：

```powershell
.\run_change.ps1 -Change 123456 -VerboseLog -OpenDashboard
```

Dashboard 当前展示：

```text
Skill Source 数量
Active Source
Content Version
Source Revision
Gerrit Patchset
Skill Event
Skill Sources 列表
Recent Gerrit Patchsets
Recent Skill Events
```

Dashboard 是展示层，MySQL 才是事实数据库。

---

# 第八部分：Skill 识别规则

## 29. Skill Root

任何 Git tracked：

```text
SKILL.md
```

所在目录为 Skill Root。

例如：

```text
skills/demo/SKILL.md
```

则：

```text
Skill Root = skills/demo
```

---

## 30. Skill Name

优先读取 `SKILL.md` YAML frontmatter 顶层：

```yaml
---
name: demo-skill
---
```

如果无法解析，POC 可以退化到目录名并产生 warning。

---

## 31. Skill Package

Skill Root 下全部 Git tracked entries 都属于 Skill Package。

例如：

```text
skills/demo/
├── SKILL.md
├── README.md
├── scripts/
├── references/
├── assets/
└── config/
```

全部进入 Digest。

因此：

```text
README 变化      -> Digest 变化
scripts 变化     -> Digest 变化
references 变化  -> Digest 变化
assets 变化      -> Digest 变化
file mode 变化   -> Digest 变化
```

Digest 层只判断内容是否一致，不判断变化是否需要重新人工审核。

---

## 32. 特殊文件

### symlink

当前：

```text
Hash Git 保存的链接目标字符串
不跟随链接
产生 warning
```

### submodule

当前：

```text
Hash gitlink commit id
不拉取子仓库
产生 warning
```

### Git LFS

当前：

```text
Hash LFS pointer
不是实际 LFS object
产生 warning
```

### binary

当前：

```text
raw bytes 正常参与 SHA-256
产生 binary warning
不一刀切禁止
```

---

# 第九部分：日常操作

## 33. 代码更新

Windows：

```powershell
git pull
.\deploy.ps1
```

Gerrit Server：

```bash
cd /opt/skillhub/gerrit-change-discovery
git pull
python3 -m pip install -r requirements.txt
python3 database.py --config config.json --check --init --summary
```

如果 `gerrit-hooks/submit` 有更新，重新执行：

```bash
cd gerrit-hooks
./install.sh $GERRIT_SITE /opt/skillhub/gerrit-change-discovery
```

---

## 34. 查看数据库统计

```bash
python3 database.py --config config.json --summary
```

---

## 35. 手工补跑 Gerrit Change

即使已经启用 Submit Hook，仍可以手工执行：

```bash
python3 main.py \
  --config config.json \
  --change 123456 \
  --verbose
```

适合：

```text
故障恢复
数据补录
POC 验证
问题排查
```

---

# 第十部分：常见故障排查

## 36. Gerrit REST 401

表现：

```text
401 Unauthorized
```

检查：

```text
username 是否为 Gerrit 用户名
HTTP Password 是否为 Gerrit HTTP Credentials 生成的密码
base_url 是否只配置 Gerrit 根路径
密码是否有特殊字符导致 config.json JSON 转义错误
```

程序认证后会自动请求 `/a/...`，不要在 `base_url` 重复添加 `/a`。

---

## 37. Gerrit REST 403

通常表示：

```text
身份可能已识别
但当前账号无目标 Change / Project 访问权限
```

具体仍需结合公司反向代理和 Gerrit Auth 模式判断。

---

## 38. SSL Certificate Verify Failed

POC 临时：

```json
"verify_ssl": false
```

正式环境：

```text
配置公司 CA
```

不要长期关闭证书验证。

---

## 39. Gerrit REST 正常，但 Git Fetch 失败

检查：

```bash
ssh -p 29418 user@gerrit.company.com
```

以及：

```bash
git clone ssh://user@gerrit.company.com:29418/team/project
```

Submit Hook 是 Gerrit 服务账号执行，因此：

> 你个人账号可以 clone，不代表 Gerrit 服务账号也可以 clone。

必须用 Gerrit 服务账号验证 SSH Key 和 known_hosts。

---

## 40. MySQL 连接失败

先执行：

```bash
python3 database.py --config config.json --check
```

检查：

```text
host
port
database
username
password
网络 ACL
MySQL bind-address
账号 Host 限制
TLS 要求
```

---

## 41. MySQL 可以连接但建表失败

如果错误类似权限不足：

```text
CREATE command denied
```

选择一种：

1. POC 给应用账号临时增加 CREATE/ALTER/INDEX；
2. DBA 手工执行 `schema.mysql.sql`，应用账号只保留 DML 权限。

正式环境推荐第 2 种。

---

## 42. Dashboard 报错但数据库已写入

Dashboard 属于展示层。

优先确认：

```bash
python3 database.py --config config.json --summary
```

如果 MySQL 数据正常，说明核心事实数据没有丢失，可以单独重跑：

```bash
python3 report_generator.py --config config.json
```

---

## 43. Submit Hook 没触发

检查：

```text
Hooks Plugin 是否安装并启用
[hooks] 配置是否正确
submitHook = submit
$GERRIT_SITE/hooks/submit 是否存在
submit 是否有 executable 权限
Hooks Plugin 是否已 reload
```

可以先把 Hook 替换为最简单测试：

```bash
#!/usr/bin/env bash
echo "$(date) $*" >> /tmp/skillhub-submit-test.log
exit 0
```

如果日志都不产生，问题在 Gerrit Hooks Plugin / 配置层，而不是 Python 项目。

---

## 44. Submit 一直等待

当前 `submit` Hook 是同步 Hook。

完整流程涉及：

```text
REST
Git fetch
Digest
MySQL
未来 Scanner
```

因此必须关注：

```text
syncHookTimeout
Git 网络耗时
Scanner 耗时
数据库耗时
```

当前 POC 使用较长 timeout 是为了验证链路。

正式生产如果安全扫描耗时很长，建议后续演进为：

```text
Patchset 阶段预扫描
Submit 阶段只查扫描状态
```

但这不影响当前 Submit-only POC 的验证。

---

# 第十一部分：安全建议

## 45. 不要提交凭据

以下文件/信息不能提交 Git：

```text
config.json
Gerrit HTTP Password
MySQL Password
SSH Private Key
公司 CA 私钥
```

建议使用：

```text
环境变量
Secret Manager
专用服务账号
```

---

## 46. 建议使用专用服务账号

正式环境不要长期依赖个人 Gerrit / MySQL 账号。

推荐：

```text
Gerrit service account
MySQL application account
专用 SSH Key
```

并遵循最小权限原则。

---

## 47. Hook Fail-open / Fail-close

当前 Submit Hook 透传 `main.py` 退出码：

```text
main.py = 0      -> Allow Submit
main.py != 0     -> Reject Submit
```

这实际上是：

```text
Fail-close
```

POC 阶段测试时要特别注意：

> Gerrit REST、MySQL、Git SSH 任何一个基础设施故障，都可能导致 Submit 被拒绝。

上线正式阻断前，应明确：

```text
哪些异常必须阻断
哪些异常只告警
是否提供紧急管理员 bypass
如何记录 bypass 审计
```

---

# 第十二部分：验收建议

## 48. 最小验收清单

建议按以下顺序完成：

1. `deploy.ps1` 成功；
2. MySQL 5 张表存在；
3. `database.py --check --summary` 正常；
4. Gerrit REST 能读取真实 Change；
5. `--no-digest` 可以识别 Changed Files；
6. Digest 能通过 SSH 读取当前 Patchset；
7. 新增 `SKILL.md` 能产生 `NEW_SKILL`；
8. 第二个 Change 只修改 scripts，仍能识别 `UPDATED_SKILL`；
9. 同一 Skill 多个 Revision 能正确累计；
10. 相同 Digest 不重复创建 Content Version；
11. 手工模拟 submit hook 返回 0；
12. Gerrit 页面实际点击 Submit 能产生 Hook 日志和 MySQL 数据；
13. 人工制造执行错误时 Submit 能被拒绝；
14. Dashboard 能展示最新数据。

---

# 第十三部分：当前已实现与未实现

## 49. 当前已经实现

```text
Gerrit Change REST
Revision Files
A/M/D/R/C Skill 识别
Baseline + Database Inventory
Skill Source
Source Revision
Content Version
SHA-256 Digest
JSON 原始证据
MySQL / SQLite
HTML Dashboard
Gerrit Submit Hook
Submit Revision/Patchset 一致性校验
```

---

## 50. 当前还未完成

当前项目仍未完整实现：

```text
正式安全 Scanner Adapter
Prompt Injection / Tool Poisoning 扫描
脚本危险行为扫描
Dependency / Secret 扫描
scan_result / finding 表
CM Review
Security Review
Exception Review
Canonical Skill 管理 UI
iflytek SkillHub API 同步
正式权限与审批 UI
定时 Reconciliation
生产级 Queue / Worker
高可用与集群部署
```

因此当前 Submit Hook 成功表示：

> 当前 Discovery / Digest / Database workflow 成功。

后续安全 Scanner 接入后，应让策略失败最终返回非 0，届时现有 Submit Hook 无需改变即可形成真正的安全门禁。

---

# 51. 推荐后续演进

```text
当前：
Submit
 -> Discovery
 -> Digest
 -> DB

下一步：
Submit
 -> Discovery
 -> Digest
 -> Scanner Adapter
 -> Scan Result
 -> Policy Decision
 -> DB
 -> Allow / Reject

后续生产：
Patchset Created
 -> Async Scan
 -> CM Review

Submit
 -> Check Approved Result
 -> Allow / Reject

Merged
 -> SkillHub Publish / Reconciliation
```

---

## 52. 常用命令速查

```text
Windows 初始化：
  .\deploy.ps1

数据库验证：
  python .\database.py --config .\config.json --check --init --summary

手工分析：
  .\run_change.ps1 -Change 123456 -VerboseLog

只验证 REST：
  .\run_change.ps1 -Change 123456 -NoDigest -VerboseLog

生成 Dashboard：
  python .\report_generator.py --config .\config.json

Baseline：
  cd ..\gerrit-skill-discovery
  .\run_batch.ps1

Linux 安装 Hook：
  cd gerrit-hooks
  ./install.sh $GERRIT_SITE /opt/skillhub/gerrit-change-discovery

Linux 手工运行：
  python3 main.py --config config.json --change 123456 --verbose
```

---

## 53. 使用原则总结

最终日常使用可以简单理解为：

```text
第一次上线：
建 MySQL
 -> 配 config.json
 -> deploy
 -> Baseline
 -> 验证真实 Gerrit Change
 -> 安装 Submit Hook

日常：
开发 Code Review
 -> 点击 Submit
 -> 自动扫描
 -> JSON
 -> MySQL
 -> Dashboard

运维：
git pull
 -> 更新依赖
 -> database --check
 -> 必要时重装 Hook
```

当前项目所有数据关系都围绕下面这条原则保持稳定：

> **Skill Source 负责“它从哪里来”，Source Revision 负责“是哪一个 Git 版本”，Content Version 负责“内容实际上是否一样”。**
