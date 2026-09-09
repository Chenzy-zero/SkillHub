---
name: auto-skill-review
description: Run or resume a security-review batch with an observable five-slot reviewer pool, safe attempt retries, and incremental Chinese localization.
allowed-tools: Bash Agent
---

# Automatic Skill Review for Claude Code

Every invocation starts from durable project state. **Do not continue an old UI task list or assume Agents from a previous invocation are still alive.** The parent only dispatches native reviewer/localizer Agents and invokes trusted project commands. Never read target packages, handoffs, scanner reports, prior AI results, Translation Memory, or batch evidence in the parent context. Do not use Git, package managers, network, MCP, or arbitrary shell commands. Never execute reviewed content. Do not inspect queue/state files yourself.

## 1. Resume checkpoint — always first

```text
Windows: cmd.exe /d /c "pytool.cmd tools\review_pool.py resume --ai-parallel 5"
Linux/CentOS/macOS: python tools/review_pool.py resume --ai-parallel 5
```

This command advances trusted static preparation when required, recovers valid orphan attempt results, replaces a stuck prior coordinator session, and returns a fresh `dispatch_session` plus newly leased `ai_dispatch.items` up to `max_parallel`. Attempt-specific result paths make redispatch safe even if an old Agent later writes its old result.

During the AI phase, reviewer-pool scheduling, result import, and refill stay inside one trusted Python process. The legacy `review.cmd --auto --json --ai-parallel 5` chain remains only for non-AI compatibility transitions such as plan/static preparation. Do not call the legacy AI-pool command directly. The former `--completed-task-id` event is replaced by `review_pool.py complete`.

`status.cmd` / `./status.sh` shows Reviewer Pool slots, task IDs, attempt numbers, runtime age, stale state, queue depth, and deferred report-projection count.

## 2. Mandatory full fan-out before waiting

For every returned item, start one fresh project Agent of type `skill-security-reviewer`, preloading `/skill-security-review`, and send only `task_id`, `handoff`, and `expected_result`.

**Launch all returned items before waiting for any one of them.** Five returned items means spawn five Agents first; do not spawn one and wait.

After each spawn succeeds:

```text
Windows: cmd.exe /d /c "pytool.cmd tools\review_pool.py launched --dispatch-session <SESSION> --task-id <TASK_ID>"
Linux/CentOS/macOS: python tools/review_pool.py launched --dispatch-session <SESSION> --task-id <TASK_ID>
```

Do not enter wait-any while a returned item is still only reserved.

## 3. Completion-driven rolling pool

Never use wait-all. Wait for any one Agent event for at most about 60 seconds.

Successful completion:

```text
Windows: cmd.exe /d /c "pytool.cmd tools\review_pool.py complete --dispatch-session <SESSION> --task-id <TASK_ID> --ai-parallel 5"
Linux/CentOS/macOS: python tools/review_pool.py complete --dispatch-session <SESSION> --task-id <TASK_ID> --ai-parallel 5
```

The task ID is the trigger. Trusted code validates it, then imports every other in-flight attempt whose result file is already durable. If five Agents finished close together, one completion command can consume all five. Launch and register every returned replacement item immediately while still-running Agents continue.

Full CSV/JSON/HTML projection is not rebuilt after each completion. While AI work remains, per-Skill durable results and the compact queue are authoritative and the report is marked dirty. The full report is rebuilt once when the Batch reaches its normal finalization boundary.

Native failed/cancelled Agent:

```text
Windows: cmd.exe /d /c "pytool.cmd tools\review_pool.py retry --dispatch-session <SESSION> --task-id <TASK_ID> --ai-parallel 5 --reason \"<SUMMARY>\""
Linux/CentOS/macOS: python tools/review_pool.py retry --dispatch-session <SESSION> --task-id <TASK_ID> --ai-parallel 5 --reason "<SUMMARY>"
```

Attempts use separate result paths. Default maximum is two; retry exhaustion alone becomes AI `INCOMPLETE`.

No Agent event during one bounded wait cycle:

```text
Windows: cmd.exe /d /c "pytool.cmd tools\review_pool.py tick --dispatch-session <SESSION> --timeout-seconds 1200 --ai-parallel 5"
Linux/CentOS/macOS: python tools/review_pool.py tick --dispatch-session <SESSION> --timeout-seconds 1200 --ai-parallel 5
```

A stale reviewer is retried safely; it is not terminal until retry exhaustion.

If this invocation itself stalls or is interrupted, the user may invoke `/auto-skill-review` again. The new invocation must restart from `resume`, not from the prior UI's in-memory task list.

Continue until `VIEW_RESULTS` / `COMPLETE`.

## 4. Incremental zh-CN localization

```text
Windows: cmd.exe /d /c "pytool.cmd tools\localize_report.py prepare --current"
Linux/CentOS/macOS: python tools/localize_report.py prepare --current
```

If `status=READY`, start one fresh `report-localizer` Agent with only `job_id`, `input_path`, `result_schema_path`, and `expected_result`. Then import:

```text
Windows: cmd.exe /d /c "pytool.cmd tools\localize_report.py import --current --job-id <JOB_ID>"
Linux/CentOS/macOS: python tools/localize_report.py import --current --job-id <JOB_ID>
```

Repeat until `COMPLETE`. Localization never changes canonical review decisions.

On unavailable isolation, malformed results, or unexpected paths, stop with the specific issue. Never reinterpret missing/failed review as PASS.
