# 《SKILL 安全管理策略》初稿

> 状态：Draft v0.1
>
> 适用对象：公司内部创建、引入、维护、发布、安装或运行的 Agent Skill / SKILL.md 资产。

## 1. 目的

建立公司统一的 Skill 安全治理机制，降低未经审查 Skill 带来的 Prompt Injection、数据泄露、恶意脚本、供应链攻击、越权工具调用、凭据泄露及生产环境误操作风险，确保所有生产使用 Skill 具备明确来源、版本、审查记录和可撤销能力。

## 2. 适用范围

适用于：

- 公司 Gerrit/GitLab/GitHub 等代码仓库中的 Skill；
- 公司员工自行创建的 Skill；
- 从互联网、开源社区、供应商或合作方引入的 Skill；
- 由 AI Agent 自动生成或修改的 Skill；
- 通过 CLI、MCP、IDE、Agent Runtime 等方式安装的 Skill；
- 包含 `SKILL.md` 及其 scripts/references/assets/配置/依赖的完整 Skill Package。

## 3. 术语

- **Skill**：以 `SKILL.md` 为核心的 Agent 能力包。
- **SkillHub**：公司统一 Skill Registry 和可信分发中心。
- **Skill Root**：包含 `SKILL.md` 的 Skill 根目录。
- **Skill Digest**：对规范化 Skill Package 内容计算的摘要，用于唯一绑定审批内容。
- **Review**：安全人工审查。
- **Scanner**：静态、依赖、Prompt、行为或 LLM 语义安全扫描器。
- **Published Version**：已通过策略门禁并上架的不可变版本。
- **Revoked**：因漏洞、误发布或策略变化被吊销的版本。

## 4. 威胁模型

需要重点防范：

1. Skill 中包含指令诱导 Agent 忽略系统规则；
2. Skill 读取并泄露 Token、SSH Key、环境变量、Cookie 等敏感信息；
3. Skill 脚本下载并执行外部 payload；
4. Skill 依赖恶意或被接管的软件包；
5. Skill 通过 MCP/工具获得超出业务需要的权限；
6. Skill 描述与实际行为不一致；
7. 已审查 Skill 在后续 commit 中被加入恶意内容；
8. 外部 Skill 通过 `latest/main` 自动更新绕过重新审查；
9. 管理员、CLI 或 API 旁路跳过安全审核；
10. 用户绕过 SkillHub 直接把外部 Skill 复制到本地 Runtime；
11. 恶意压缩包、符号链接、路径穿越等导入攻击；
12. 扫描器误报/漏报导致错误放行。

## 5. 管理原则

### 5.1 统一注册

生产或受控研发环境中使用的 Skill 必须：

- 来自公司 SkillHub；或
- 已在 SkillHub 中登记并具备有效 `APPROVED/PUBLISHED` 状态。

### 5.2 默认不信任

外部来源、个人本地目录、历史仓库未登记 Skill 默认状态为 `UNTRUSTED/DISCOVERED`。

### 5.3 审批绑定内容而不是名字

审批必须绑定：

```text
skill_id + skill_digest + policy_version
```

不得仅以 Skill 名称、路径或 commit id 作为“已审查”的依据。

### 5.4 发布版本不可变

已发布版本不得原地修改。任何修改必须创建新版本或新 digest。

### 5.5 纵深防御

至少包含：

```text
来源控制
  + 包格式校验
  + 自动扫描
  + 人工复核
  + 发布门禁
  + 安装来源限制
  + 运行时最小权限
  + 审计与撤销
```

## 6. 角色与职责

| 角色 | 主要职责 |
| --- | --- |
| Skill Author | 创建/修改 Skill、填写元数据、修复扫描问题 |
| Skill Owner | 对 Skill 业务用途、维护责任和风险承担负责 |
| CM | 资产登记、版本治理、审核流程运营、状态维护 |
| Security Reviewer | 对中高风险 Skill 完成人工安全审查 |
| SkillHub Admin | 平台配置与运维，不应默认拥有安全审批豁免 |
| Auditor | 查看审计、历史、审批证据，不直接修改 Skill |
| Runtime Admin | 配置 Agent/CLI 可信 Registry 和运行时策略 |

高风险/特权 Skill 建议实行双人审批或至少“Author 与最终审批人分离”。

## 7. Skill 风险分级

### L0 — 低风险

特征：

- 纯 Markdown；
- 不执行脚本；
- 不读写敏感文件；
- 不访问网络；
- 不调用高权限工具。

策略：自动扫描通过后可进入快速审核流程。

### L1 — 中风险

特征：

- 读取普通项目文件；
- 使用只读 MCP；
- 生成模板、报告；
- 对仓库执行非破坏性分析。

策略：自动扫描 + 人工抽检或指定 Owner 审批。

### L2 — 高风险

特征：

- Shell/Python/PowerShell/Node 等脚本；
- 写文件；
- 访问网络；
- 调用 Git、CI/CD、数据库；
- 修改配置；
- 使用可产生副作用的 MCP。

策略：必须人工安全审查。

### L3 — 特权

特征：

- 访问生产环境；
- 读取或使用凭据；
- 部署/删除/权限变更；
- 账号管理；
- 跨系统写操作；
- 对大量资产产生不可逆影响。

策略：双人审批/安全负责人批准、强审计、最小权限、定期复审。

## 8. 来源与准入

### 8.1 内部 Skill

来源必须能追溯到公司 SCM：

- repository；
- branch；
- path；
- author；
- Change-Id/commit；
- digest。

### 8.2 外部 Skill

禁止生产 Runtime 直接从公网安装。

标准流程：

```text
外部源
 -> 固定 tag/commit/digest
 -> 导入 Quarantine
 -> License/来源检查
 -> 自动扫描
 -> 人工审查（按风险）
 -> 内网重打包/镜像
 -> SkillHub 发布
```

不得跟随 `main/latest` 自动升级。

## 9. 注册要求

每个 Skill 至少记录：

- skill_id；
- name；
- namespace；
- owner/team；
- description；
- source_type；
- repository；
- branch；
- skill_path；
- source revision；
- skill_digest；
- risk_level；
- lifecycle_status；
- created_at / updated_at。

## 10. 包格式要求

第一阶段建议：

- 必须有 `SKILL.md`；
- frontmatter 满足 Agent Skills 规范；
- 限制单文件和包总体大小；
- 限制文件数；
- 禁止绝对路径和路径穿越；
- 禁止指向 Skill Root 外部的 symlink；
- 默认禁止 Git submodule；
- LFS 文件必须取回真实内容后审查；
- 可执行二进制默认禁止，确需使用走例外审批；
- 允许扩展名采用白名单策略。

## 11. 自动扫描要求

自动扫描至少覆盖：

### 11.1 Schema / Metadata

- `SKILL.md` 存在性；
- YAML frontmatter；
- name/description；
- 目录边界；
- 文件类型；
- 文件大小；
- 编码/Unicode 异常。

### 11.2 Prompt 风险

- Prompt Injection；
- System Prompt 泄露诱导；
- 凭据读取；
- 数据外传；
- 安全控制绕过；
- Agent Memory/Config 污染；
- MCP Tool Poisoning；
- 过度自主执行。

### 11.3 代码风险

- exec/eval；
- subprocess/shell；
- curl/wget 下载执行；
- 网络连接；
- 文件删除；
- 权限修改；
- 环境变量/密钥读取；
- 反序列化；
- 混淆/编码 payload。

### 11.4 供应链风险

- 依赖固定版本；
- CVE；
- 不存在/可疑包；
- 外部 URL；
- 安装脚本；
- SBOM（L2/L3 推荐强制）。

## 12. 人工安全审查要求

人工 Reviewer 至少确认：

1. Skill 的描述与真实行为一致；
2. 所请求权限符合最小权限；
3. 所有脚本行为可解释；
4. 外部网络目的地合理；
5. 不读取无关敏感数据；
6. 不下载并执行不固定内容；
7. 依赖来源可信；
8. 未发现 Prompt/Tool 注入；
9. 扫描器发现均已处理或有风险接受；
10. 高风险操作有显式确认/保护机制。

## 13. 审核结论

可用状态：

- `APPROVED`
- `REJECTED`
- `APPROVED_WITH_EXCEPTION`

风险接受必须记录：

- exception_id；
- 风险描述；
- 影响；
- 责任人；
- 批准人；
- 到期时间；
- 补偿控制。

## 14. 发布要求

只有满足以下条件才允许发布：

- digest 与审批记录一致；
- 自动扫描通过策略；
- 所需人工审批已完成；
- 依赖/许可证满足要求；
- 版本号合法；
- 无未关闭 Critical/High finding（除非正式风险接受）；
- 未被 revoke；
- 发布入口经过统一 Policy Engine。

## 15. 重新审查触发条件

以下情况至少重新自动扫描：

- Skill Package 任意内容 digest 变化；
- 扫描规则/策略发生重大升级；
- Scanner 新增关键检测能力；
- 发现影响该 Skill 的新漏洞；
- 依赖版本变化；
- 外部来源版本变化。

以下情况必须重新人工审查：

- `SKILL.md` 核心行为改变；
- 新增/修改脚本；
- 新增网络访问；
- 新增工具/MCP；
- 权限扩大；
- 风险级别上升；
- Critical/High finding；
- L3 定期复审到期。

## 16. 下架与吊销

发现下列情况时可立即 `REVOKED/OFFLINE`：

- 恶意行为；
- 高危漏洞；
- 凭据泄露；
- 来源被接管；
- 依赖供应链事件；
- 误发布；
- Owner 无法继续维护且风险不可接受。

Runtime 应定期同步撤销列表，至少在安装/更新时阻止被吊销 digest。

## 17. 运行时来源控制

仅建设 SkillHub 不足以形成强安全策略。

应配套：

- 公司统一 Agent/CLI 配置；
- 默认 Registry 指向内网 SkillHub；
- 禁止或告警公网 Registry；
- 安装时校验 digest；
- 高成熟阶段增加公司签名验证；
- 定期扫描本地 Agent Skill 目录，识别未登记来源；
- 关键环境通过终端策略限制旁路安装。

## 18. 审计与证据留存

至少记录：

- Skill 创建/修改/导入；
- revision/digest；
- scanner/version；
- policy/version；
- findings；
- Reviewer；
- 审核意见；
- 上架/下架/撤销；
- 风险接受；
- 安装/下载（按公司隐私和审计政策）；
- 管理员操作。

建议审核证据保留周期与公司软件供应链/研发审计标准对齐。

## 19. KPI / SLA 建议

- 已发现 Skill 登记覆盖率；
- 已审查 Skill 比例；
- 未审查生产 Skill 数量；
- Critical/High finding 数量；
- 平均审查时长；
- Skill 变更后重新扫描时延；
- 被撤销版本安装阻断率；
- 外部 Skill 未经内网镜像的旁路发现数；
- 审核超期数量；
- Scanner 可用率。

## 20. 首版落地边界

第一阶段先做到：

1. 全量发现 Gerrit Skill；
2. 统一登记；
3. 内容变化自动变 `STALE`；
4. 自动扫描；
5. CM/安全人工审核；
6. SkillHub 上架；
7. 生产只安装已批准版本。

签名、沙箱动态执行、复杂 SBOM、端点强制策略可分后续阶段逐步增强。
