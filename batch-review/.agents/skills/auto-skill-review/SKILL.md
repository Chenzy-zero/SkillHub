---
name: auto-skill-review
description: Run a prepared security-review batch with trusted scripts and up to five isolated Skill reviewer subagents. Scripts select tasks, import results, and generate reports.
---

# Automatic Skill Review for Codex CLI

The parent only dispatches native reviewer subagents. Never read target packages,
handoff contents, package-manifest.json, static reports, prior AI reports, or batch
evidence in the parent context. Do not use Git, package managers, network, MCP, or
arbitrary shell commands. Never execute reviewed content.

## Single script checkpoint

From this project root, call:

```text
Windows: cmd.exe /d /c "review.cmd --auto --json --ai-parallel 5"
Linux/CentOS/macOS: ./review.sh --auto --json --ai-parallel 5
```

The script performs all deterministic transitions to the next AI boundary or
completion. It handles download, static scans, ai-review-queue.json, file existence
checks, validation, import, reports, and cleanup. It returns only compact control
JSON; logs remain in log_path. Do not call status or inspect queues separately.
The dispatch limit overrides older configuration defaults without rewriting a
frozen batch. Lower it if the operator requests or the client supports fewer agents.

## Reviewer dispatch

1. If exit_code is nonzero, stop and report the issue, next_instruction, and
   log_path. Do not retry in a loop or silently initialize/install software.
2. For next_action AI_REVIEW, consume ai_dispatch.items in the supplied order.
   The script has excluded existing results and prioritized larger packages.
3. Start one fresh project subagent named `skill_security_reviewer` per item.
   It follows `$skill-security-review` and the canonical policy. Send only
   task_id, handoff, and expected_result. Never combine Skills in one context,
   review inline, or substitute a generic agent without the review policy.
4. Keep up to ai_dispatch.max_parallel reviewers running (normally 5). Use
   completion events: as soon as one finishes, fill its slot with the next item.
   Do not wait for a whole group of five before refilling. Respect actual client
   limits and report any lower effective concurrency. Do not poll status/files.
5. Track only task IDs and completion metadata. Once all dispatched reviewers
   finish, call the same script checkpoint. The script imports results and
   advances to the next repository. Repeat until VIEW_RESULTS or COMPLETE.
   If a task just reported complete but is dispatched again, its result is
   missing: stop and report that task; do not blindly launch it again.
6. On completion, report only batch_id and result_paths, without opening reports.

On unavailable isolation, agent failure, malformed results, unexpected paths, or
additional authority requirements, stop with the specific issue (use
CONTEXT_ISOLATION_UNAVAILABLE when applicable). Do not skip failed tasks.
