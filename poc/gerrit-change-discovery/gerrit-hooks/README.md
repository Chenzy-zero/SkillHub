# Gerrit Submit Hook

这个目录用于把 `gerrit-change-discovery` 挂到 Gerrit **Submit** 动作上。

目标行为：

```text
用户点击 Submit
      ↓
Gerrit Hooks Plugin: submit
      ↓
执行 gerrit-change-discovery/main.py
      ↓
Gerrit Changed Files / Skill Resolver / Digest / Database / Scan
      ↓
main.py exit 0     -> 允许 Submit
main.py exit != 0  -> 拒绝 Submit
```

## 1. Gerrit Hooks Plugin

这不是普通 Git repository hook，需要 Gerrit Hooks Plugin 支持同步 `submit` hook。

在 `$GERRIT_SITE/etc/gerrit.config` 中配置：

```ini
[hooks]
    path = hooks
    submitHook = submit
    syncHookTimeout = 180
```

`180` 秒仅用于当前 POC 直接在 Submit 时执行完整扫描。后续请根据实际耗时调整。

## 2. 部署 POC

例如将目录部署到：

```text
/opt/skillhub/gerrit-change-discovery
```

确保其中存在：

```text
main.py
config.json
database.py
change_analyzer.py
skill_digest.py
...
```

Gerrit 服务用户必须能读取配置、写 output/workspace，并访问 Gerrit REST、Git SSH 和 MySQL。

## 3. 安装 Hook

例如 Gerrit Site：

```text
/var/gerrit/review_site
```

执行：

```bash
cd /opt/skillhub/gerrit-change-discovery/gerrit-hooks
chmod +x install.sh submit
./install.sh /var/gerrit/review_site /opt/skillhub/gerrit-change-discovery
```

最终：

```text
/var/gerrit/review_site/hooks/submit
```

必须具有执行权限。

## 4. Hook 参数

Hooks Plugin 会调用类似：

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

当前 hook 会把 `change / patchset / commit` 传给 `main.py`，并使用：

```text
--expected-revision
--expected-patchset
```

校验 Gerrit REST 返回的 current revision 与 Submit 时真正提交的 Patchset 完全一致，避免误扫其他版本。

## 5. 手工模拟 Hook

正式点击 Submit 前，可以在 Gerrit 服务器上用真实参数模拟：

```bash
/var/gerrit/review_site/hooks/submit \
  --change Ixxxxxxxxxxxxxxxx \
  --project team/skills \
  --branch develop \
  --submitter test \
  --submitter-username test \
  --patchset 3 \
  --commit abcdef1234567890abcdef1234567890abcdef12

echo $?
```

`0` 代表允许 Submit；非 `0` 代表 Gerrit 会拒绝 Submit。

## 6. 路径覆盖

默认：

```text
POC_HOME=/opt/skillhub/gerrit-change-discovery
PYTHON=/usr/bin/python3
CONFIG=/opt/skillhub/gerrit-change-discovery/config.json
LOG_DIR=/opt/skillhub/gerrit-change-discovery/output/hooks
```

也可以通过 Gerrit 服务进程环境变量覆盖：

```text
SKILLHUB_POC_HOME
SKILLHUB_PYTHON
SKILLHUB_CONFIG
SKILLHUB_HOOK_LOG_DIR
```

## 7. 日志

每次 Submit 会写：

```text
output/hooks/submit-<project>-<change>-<patchset>.log
```

扫描执行失败时，hook 会把最近 20 行日志输出给 Gerrit 用户，并返回非 0，从而拒绝 Submit。

## 8. 当前阻断语义

当前 hook 直接透传 `main.py` 的退出码：

- `0`：当前 discovery/digest/database workflow 执行成功；
- 非 `0`：流程异常，拒绝 Submit。

后续接入真正的安全 Scanner 后，应让策略判定 `FAILED/REJECTED` 时让 `main.py` 返回非 0，这样无需修改 submit hook 即可形成正式安全门禁。
