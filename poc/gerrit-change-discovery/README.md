# Gerrit Change Skill Discovery POC

这个目录是 Skill 安全治理的日常增量 POC。核心入口不是全仓扫描，而是 **Gerrit Code Review / Patchset 的 Changed Files**。

## 当前链路

```text
Gerrit Code Review
        ↓
读取 current patchset
        ↓
获取 Revision Files
        ↓
A / M / D / R / C
        ↓
新增 SKILL.md / 命中已有 Skill Root
        ↓
Affected Skills
        ↓
仅 fetch 当前 Patchset
        ↓
仅计算受影响 Skill Root Digest
        ↓
MySQL / SQLite 存档
        ↓
JSON 原始证据 + HTML Dashboard
```

历史 Baseline 仍由 `../gerrit-skill-discovery/` 负责；本目录负责日常 Gerrit 增量处理。

## 目录

```text
poc/gerrit-change-discovery/
├── main.py
├── gerrit_client.py
├── change_analyzer.py
├── inventory.py
├── skill_digest.py
├── database.py
├── report_generator.py
│
├── schema.sql                 # SQLite schema
├── schema.mysql.sql           # MySQL 5 张核心表
├── create_database.mysql.sql  # MySQL 建库辅助 SQL
├── requirements.txt           # PyMySQL
│
├── config.example.json
├── deploy.ps1
├── deploy.bat
└── run_change.ps1
```

## MySQL 推荐方案

推荐在现有 MySQL Server 中单独建立：

```text
skillhub_security
```

第一次由 DBA / 管理账号执行：

```sql
CREATE DATABASE IF NOT EXISTS skillhub_security
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
```

也可以直接执行：

```text
create_database.mysql.sql
```

应用启动后会自动执行 `schema.mysql.sql`，在目标 Database 中创建/确认以下 5 张核心表：

```text
skill_source
source_revision
content_version
gerrit_patchset
change_skill_event
```

数据库只需要提前存在；表可以由 `deploy.ps1` 自动创建。

## 配置

第一次：

```powershell
Copy-Item .\config.example.json .\config.json
```

MySQL 配置示例：

```json
"database": {
  "type": "mysql",
  "host": "192.168.1.100",
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
}
```

POC 可以直接填写：

```json
"password": "your_mysql_password"
```

正式一点则保持 `password` 为空，并在 PowerShell 设置：

```powershell
$env:SKILLHUB_DB_PASSWORD="your_mysql_password"
```

`database_path` 只在 `database.type=sqlite` 时使用；MySQL 模式会忽略它。

## 一键部署

先确保目标 MySQL Database 已存在，然后：

```powershell
.\deploy.ps1
```

部署会自动：

```text
检查 Python / Git
安装 PyMySQL
校验 Python 语法
保留已有 config.json
检查 MySQL 连接
执行 schema.mysql.sql
生成/确认 5 张表
读取数据库统计
生成 Dashboard
```

数据库连接和建表也可以单独验证：

```powershell
python .\database.py --config .\config.json --check --init --summary
```

成功时会显示类似：

```text
Database connection OK: mysql://skillhub_app@192.168.1.100:3306/skillhub_security
Database schema initialized: mysql://skillhub_app@192.168.1.100:3306/skillhub_security
```

## 日常处理 Gerrit 单据

例如 Change 123456：

```powershell
.\run_change.ps1 -Change 123456 -VerboseLog
```

程序自动完成：

```text
Gerrit Change
 -> Changed Files
 -> Skill 识别
 -> Skill Root Digest
 -> JSON
 -> MySQL
 -> Dashboard
```

不需要再单独导入数据库。

## 数据模型

### skill_source

记录一个 Skill 来源：

```text
repository + skill_path + skill_name
```

### source_revision

记录每个 Source 的 Gerrit/Git revision：

```text
Source
 -> commit/revision
 -> change_number / patchset
 -> Content Version
```

### content_version

使用完整 Skill Package SHA-256 Digest 表示内容版本。

```text
commit A -> digest X
commit B -> digest Y
commit C -> digest Y
```

即 3 个 Source Revision、2 个 Content Version。

### gerrit_patchset

保存 Gerrit Change / Patchset、Changed Files 和原始分析结果。

### change_skill_event

记录本 Patchset 对 Skill 产生的事件：

```text
NEW_SKILL
UPDATED_SKILL
DELETED_SKILL
RENAMED_SKILL
COPIED_SKILL
```

## Inventory

增量分析会合并：

```text
Baseline JSON Inventory
        +
数据库中 ACTIVE skill_source
        ↓
当前 Inventory
```

因此首次发现 `SKILL.md` 后会写入数据库；以后只修改该 Skill 下的脚本或 references，即使 Change 文件列表中没有 `SKILL.md`，也能通过数据库识别为 `UPDATED_SKILL`。

## Dashboard 与 JSON

```text
MySQL / SQLite = 结构化事实存档
JSON           = 每次分析的原始证据
HTML Dashboard = 展示层
```

Dashboard 默认：

```text
output/dashboard/index.html
```

JSON 默认：

```text
output/change-<change>-patchset-<n>.json
```

## 数据库权限

POC 自动建表时，应用账号建议至少拥有目标 Database 上的：

```text
SELECT
INSERT
UPDATE
DELETE
CREATE
ALTER
INDEX
```

等表结构稳定后，正式运行账号可以收敛为：

```text
SELECT
INSERT
UPDATE
DELETE
```

DDL 由 DBA 单独管理。

## 下一阶段

数据库链路验证完成后，下一步是把手工：

```text
run_change.ps1 -Change xxx
```

升级为：

```text
Gerrit patchset-created
        ↓
自动触发 main workflow
        ↓
MySQL
        ↓
Scanner / CM Review / SkillHub
```
