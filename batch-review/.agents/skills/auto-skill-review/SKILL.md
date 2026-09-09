---
name: auto-skill-review
description: Run or resume a security-review batch with an observable five-slot reviewer pool, safe attempt retries, and incremental Chinese localization.
---

# Automatic Skill Review for Codex CLI

Every invocation starts from durable project state. **Do not continue an old UI task list or assume reviewers from a previous invocation are still alive.** The parent only dispatches native reviewer/localizer subagents and invokes trusted project commands. Never read target packages, handoffs, scanner reports, prior AI results, Translation Memory, or batch evidence in the parent context. Do not use Git, package managers, network, MCP, or arbitrary shell commands. Never execute reviewed content. Do not inspect queue/state files yourself.

## 1. Resume checkpoint — always first

Call exactly one of:

```text
Windows: cmd.exe /d /c "pytool.cmd tools\review_pool.py resume --ai-parallel 5"
Linux/CentOS/macOS: python tools/review_pool.py resume --ai-parallel 5
```

This trusted command advances plan/static preparation when needed, recovers valid late/orphan attempt results, replaces any prior coordinator session, and returns a fresh `dispatch_session` plus only newly leased `ai_dispatch.items` up to `max_parallel`. Reviewer attempts use isolated result paths, so replacing a stuck coordinator is safe even if an old reviewer writes late.

Compatibility note: the pool controller wraps the legacy `review.cmd --auto --json --ai-parallel 5` checkpoint. Do not call that legacy AI-pool command directly. The former `--completed-task-id` completion event is replaced by `review_pool.py complete` below.

For diagnostics the operator can run `status.cmd` / `./status.sh`; it shows Reviewer Pool slots, task IDs, attempt numbers, runtime age, stale state, and queue depth.

## 2. Mandatory full fan-out before waiting

For **every** returned `ai_dispatch.items` entry, start one fresh project subagent named `skill_security_reviewer`, following `$skill-security-review`, with only `task_id`, `handoff`, and `expected_result`.

**Do not wait after starting the first reviewer. Launch all returned items first.** If five items are returned, the required sequence is spawn 1, spawn 2, spawn 3, spawn 4, spawn 5, then wait-any.

Immediately after each native spawn succeeds, register it:

```text
Windows: cmd.exe /d /c "pytool.cmd tools\review_pool.py launched --dispatch-session <SESSION> --task-id <TASK_ID>"
Linux/CentOS/macOS: python tools/review_pool.py launched --dispatch-session <SESSION> --task-id <TASK_ID>
```

Do not enter a wait cycle while any returned item is still only reserved/unlaunched.

## 3. Completion-driven rolling pool

Never use wait-all. Wait for any one native reviewer event for at most about 60 seconds.

On success:

```text
Windows: cmd.exe /d /c "pytool.cmd tools\review_pool.py complete --dispatch-session <SESSION> --task-id <TASK_ID> --ai-parallel 5"
Linux/CentOS/macOS: python tools/review_pool.py complete --dispatch-session <SESSION> --task-id <TASK_ID> --ai-parallel 5
```

The trusted importer validates that attempt result, finalizes only that Skill, releases one slot, refreshes reports, and returns replacement `ai_dispatch.items`. Launch every replacement immediately and register `launched` before waiting again.

On native failed/cancelled reviewer, retry safely instead of failing the Skill immediately:

```text
Windows: cmd.exe /d /c "pytool.cmd tools\review_pool.py retry --dispatch-session <SESSION> --task-id <TASK_ID> --ai-parallel 5 --reason \"<SUMMARY>\""
Linux/CentOS/macOS: python tools/review_pool.py retry --dispatch-session <SESSION> --task-id <TASK_ID> --ai-parallel 5 --reason "<SUMMARY>"
```

Attempt paths are isolated. Default maximum is two attempts; only retry exhaustion becomes AI `INCOMPLETE`.

If no reviewer emits an event during a bounded wait cycle, run:

```text
Windows: cmd.exe /d /c "pytool.cmd tools\review_pool.py tick --dispatch-session <SESSION> --timeout-seconds 1200 --ai-parallel 5"
Linux/CentOS/macOS: python tools/review_pool.py tick --dispatch-session <SESSION> --timeout-seconds 1200 --ai-parallel 5
```

A non-stale tick only reconciles/refills. A reviewer exceeding the runtime limit is safely retried; retry exhaustion alone becomes terminal `INCOMPLETE`.

If this parent invocation itself gets stuck or is interrupted, the user may simply invoke `$auto-skill-review` again. The new invocation must restart at `review_pool.py resume`; never reuse the previous invocation's in-memory task state.

Continue until the trusted response reaches `VIEW_RESULTS` / `COMPLETE`.

## 4. Incremental zh-CN localization

Do not open the report before localization. Prepare the next immutable report-safe translation job:

```text
Windows: cmd.exe /d /c "pytool.cmd tools\localize_report.py prepare --current"
Linux/CentOS/macOS: python tools/localize_report.py prepare --current
```

If `status=READY`, start one `report_localizer` following `$report-zh-localizer`, sending only `job_id`, `input_path`, `result_schema_path`, and `expected_result`. After it completes:

```text
Windows: cmd.exe /d /c "pytool.cmd tools\localize_report.py import --current --job-id <JOB_ID>"
Linux/CentOS/macOS: python tools/localize_report.py import --current --job-id <JOB_ID>
```

Repeat until localization returns `COMPLETE`. Localization failure is presentation-only and never changes canonical security, quality, score, or final approval.

On unavailable isolation, malformed results, or unexpected paths, stop with the specific issue. Never reinterpret a missing/failed review as PASS.
