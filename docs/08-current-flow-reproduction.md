# 当前 Skill 安全审查流程草图复现与说明

本文将最初方案图中的关键结构转成可版本化的 Mermaid/Markdown，便于后续评审和修改。

## 1. 当前 `skill_summary` 草图

| SKILL名称 | 仓库名称 | SKILL所在路径 | 最新commitid | 是否经过安全审查 |
| --- | --- | --- | --- | --- |
| jira-query-skill | Skill-CM | JIRA相关查询/jira-query-skill | 示例 revision | 是 |

## 2. 当前 `skill_history` 草图

| SKILL名称 | 仓库名称 | SKILL所在路径 | commitid | 更新序号 |
| --- | --- | --- | --- | ---: |
| jira-query-skill | Skill-CM | JIRA相关查询/jira-query-skill | 示例 revision | 1 |

## 3. 当前流程复现

```mermaid
flowchart LR
  A[git代码提交] -->|钩子触发| B[识别提交文件]
  B --> C{文件中是否存在已记录的SKILL}

  C -->|是| D[复制该SKILL记录至SKILL历史日志表]
  D --> E[更新对应SKILL的最新commitid]
  E --> F[将是否经过安全审查设置为否]

  C -->|否| G{文件中是否存在SKILL.md}
  G -->|是| H[确认该SKILL相关信息]
  H --> I[将该SKILL记录至SKILL详情表]
  I --> J[将是否经过安全审查设置为否]
```

## 4. 原方案判断逻辑

原方案在“是否属于已登记 Skill”处考虑：

1. 判断仓库是否为已有 Skill 的仓库；
2. 判断第一层路径是否一致；
3. 逐层判断。

## 5. 建议升级方向

原流程建议升级为：

```mermaid
flowchart LR
  A[Gerrit事件] --> B[解析changed files: A/M/D/R/C]
  B --> C[新旧路径向上解析Skill Root]
  C --> D{是否发现受影响Skill}
  D -->|新Skill| E[登记Skill资产]
  D -->|已有Skill| F[创建新Skill Version]
  E --> G[计算完整目录digest]
  F --> G
  G --> H[自动安全扫描]
  H --> I{Policy Gate}
  I -->|需人工| J[进入Review Queue]
  I -->|自动通过| K[APPROVED]
  J --> L[人工审查]
  L --> K
  K --> M[允许SkillHub发布]
```

核心变化：

- 不再只看“新增文件是否包含 `SKILL.md`”；
- 不再只把 commitid 作为审查版本；
- 改为完整 Skill Package 的 digest；
- 支持 scripts-only change、rename、delete、多 Skill commit；
- Boolean 审查字段升级为完整状态机；
- 扫描、审核、发布分别留不可变记录。
