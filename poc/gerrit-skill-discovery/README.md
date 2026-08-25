# Gerrit Skill Discovery POC v0.1

这个 POC 用来验证当前 Skill 安全管理策略最核心的几个假设：

1. 每个 Gerrit Patchset 都可以触发服务端检查；
2. `SKILL.md` 作为 Skill 识别锚点，所在目录作为 Skill Root；
3. Skill Root 下所有 Git tracked 内容构成 Skill Package；
4. `repository + skill_path + skill_name` 作为首版 Skill Source Key；
5. Commit SHA 作为 Source Revision，整个 Skill Package 的 SHA-256 作为 Content Version 身份；
6. 同一内容即使 Commit 不同，也可以通过相同 Digest 识别出来。

> 注意：Gerrit **不会执行仓库里的标准 Git hooks**。要在 Gerrit 服务端按 Patchset 触发，应使用 Gerrit hooks plugin、`stream-events` 消费者或自研 Gerrit 插件。本 POC 按 hooks plugin 的 `patchset-created` 参数实现。

## 文件

- `skill_scan.py`：扫描任意 Git revision，可直接用于 Baseline；支持普通仓库和 bare repository。
- `gerrit_hook.py`：接收 Gerrit hooks plugin 的 `patchset-created` 参数并调用扫描器。
- `patchset-created`：Gerrit hooks plugin 的示例 shell wrapper。

## POC 当前策略

### Skill Root

任何名为 `SKILL.md` 的 tracked file 所在目录都视为一个 Skill Root。

### Skill Name

优先读取 `SKILL.md` YAML frontmatter 顶层的 `name:`。POC 为保持零依赖，只实现了简单的 `name:` 提取；未找到时退化为目录名并产生 warning。

### Skill Package

当前直接包含 Skill Root 下的**全部 Git tracked entries**，不做 README、references、assets 等 ignore。

这意味着：

- README 变化 -> Digest 变化；
- scripts 变化 -> Digest 变化；
- references/assets 变化 -> Digest 变化；
- 文件权限 mode 变化 -> Digest 变化。

“变化是否需要重新人工审核”不在 Digest 层判断，后续交给 Policy/Review 层。

### Source Key

```text
repository | skill_path | skill_name
```

Branch 暂不进入 Source Key，因为当前公司 Gerrit 分支管理并不严格，所有 Patchset 都先进入发现流程。Branch 仍作为来源元数据保存。

### Content Digest

对 Skill Package 中的 tracked entries 按相对路径排序，每项记录：

```text
git_mode + NUL + relative_path + NUL + sha256(raw_content) + LF
```

最后对整个 manifest 再执行 SHA-256：

```text
skill_digest = SHA256(manifest)
```

因此 Commit SHA 和 Content Digest 分工如下：

- Commit/Revision：回答“这份 Skill 来自哪次 Gerrit 提交”；
- Skill Digest：回答“这份 Skill Package 的内容是不是同一版本”。

## 特殊文件目前怎么处理

POC **不直接阻断** symlink、submodule、Git LFS 或 binary，而是尽可能计算摘要并输出 warning，方便拿真实仓库数据后再定公司策略。

### symlink

Git 中符号链接的 mode 是 `120000`，仓库保存的是“链接目标字符串”，而不是目标文件内容。

POC：

- 对链接目标字符串计算摘要；
- 不跟随链接；
- 输出 warning。

生产策略后续建议至少禁止“指向 Skill Root 外”的 symlink，避免扫描对象与实际运行对象不一致。

### submodule

Git submodule 在父仓库中只保存一个 gitlink（mode `160000`）和子仓库 commit id，**父仓库并不包含子仓库实际文件**。

POC：

- 将 gitlink commit id 纳入 Digest；
- 输出 warning；
- 不自动拉取子仓库。

如果生产环境要允许 submodule，必须额外拉取固定 commit 后递归扫描，否则“扫描完整 Skill Package”这个目标无法成立。

### Git LFS

Git 仓库里通常只保存一个 LFS pointer，真实大文件位于 LFS 存储。

POC：

- 识别常见 LFS pointer；
- 当前只对 pointer 计算摘要；
- 输出 warning。

生产策略如果允许 LFS，应在扫描前获取真实 LFS object，然后对真实内容进行 Hash/扫描。

### Binary

Binary 本身不等于恶意文件，例如图片、PDF、模型文件都可能合理；真正的问题是很多文本型安全扫描器无法理解 `.exe/.dll/.so/.jar/压缩包` 等内容。

POC：

- 对二进制 raw bytes 正常计算 SHA-256；
- 粗略检测包含 NUL byte 的 blob 并产生 warning；
- 不做阻断。

建议生产策略不要“一刀切禁止所有 binary”，而是后续按类型做白名单/扫描规则。

## 1. Baseline 测试

在 Gerrit 服务器或任意能访问仓库的机器上：

```bash
python3 skill_scan.py \
  --repo /var/gerrit/review_site/git/team/project.git \
  --revision HEAD \
  --repository-name team/project \
  --no-manifest
```

输出示例：

```json
{
  "repository": "team/project",
  "revision": "abc123...",
  "skill_name": "jira-query",
  "skill_path": "skills/jira-query",
  "source_key": "team/project|skills/jira-query|jira-query",
  "skill_digest": "...",
  "digest_algorithm": "SHA-256"
}
```

## 2. Gerrit patchset-created POC

Gerrit hooks plugin 的 `patchset-created` 是异步 Hook，每创建一个 Patchset 都会触发，因此适合当前“所有提交都检查，但第一阶段不阻塞提交”的策略。

将三个文件放到例如：

```text
/opt/skillhub-poc/
```

并把 `patchset-created` wrapper 放到 hooks plugin 配置的 hook path。

默认示例路径：

```text
SKILL_POC_HOME=/opt/skillhub-poc
SKILL_POC_GIT_BASE=/var/gerrit/review_site/git
SKILL_POC_OUTPUT=/var/gerrit/review_site/logs/skill-poc.jsonl
```

也可以用环境变量覆盖。

测试时可以手工模拟 Gerrit 调用：

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

## 当前 POC 的刻意简化

v0.1 每次 Patchset 都扫描该 revision 中的所有 Skill，以优先验证数据模型和 Digest 是否正确。

它还没有实现：

- changed files A/M/D/R/C；
- 只扫描受影响 Skill Root；
- Delete/Rename old revision 解析；
- 数据库；
- Canonical Skill 合并；
- Scanner；
- SkillHub API；
- 队列/重试。

如果 Baseline 和 Digest 模型通过真实仓库验证，v0.2 再增加：

```text
Patchset
 -> parent/current diff
 -> changed files
 -> affected Skill Root
 -> Source Revision
 -> Content Version
 -> DB
```

这样可以避免第一版同时验证太多变量。
