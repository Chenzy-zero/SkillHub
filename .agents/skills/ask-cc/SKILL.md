---
name: ask-cc
description: Inspect the Skill security-review project's small status output and tell the operator the single next action without opening target Skills or reports.
---

# Ask the project

Use the trusted status wrapper only:

```text
Windows: cmd.exe /d /c "batch-review\status.cmd --json"
Linux/CentOS/macOS: ./batch-review/status.sh --json
```

Do not read target Skill directories, handoffs, scanner reports, manifests, batch
evidence, or AI result JSON. Explain the current state and one next action.

When status is ready for automated execution, tell the operator to invoke
`$auto-skill-review`. For setup states, name the one configuration, scanner, or
initialization action reported by the status command.
