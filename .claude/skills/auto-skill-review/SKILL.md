---
name: auto-skill-review
description: Run the prepared Skill security-review batch from one Claude Code invocation, using trusted scripts for all deterministic work and isolated Agents for Skill content review.
allowed-tools: Read Bash Agent
---

# Automatic Skill Review

This is the coordinator for the security-review workflow. It can be invoked
directly after `batch-review` has been initialized, configured, and passed the
scanner health check; the operator does not need to run `review.cmd` first.
The parent conversation is only a coordinator. It must not inspect Skill files,
manifests, static scanner reports, or prior AI reports.
Do not read `package-manifest.json`, `raw-report.json`, `normalized-result.json`,
handoff contents, or any existing AI result in the parent conversation.

The coordinator may call only the trusted launchers under `batch-review/`.
Those scripts perform CSV validation, repository download, Skill extraction,
static scanning, state changes, result merging, and cleanup. The coordinator
does not publish, push, install packages, or execute reviewed content.

Do not use Git, package managers, web access, MCP, or arbitrary shell commands
during this workflow. Bash is limited to the repository's `review` and
read-only `status` wrappers described below.

## Coordinator loop

1. From the repository root, check status:

   ```text
   Windows: cmd.exe /d /c "batch-review\status.cmd --json"
   Linux/CentOS/macOS: ./batch-review/status.sh --json
   ```

2. If the status is `PLAN`, `START`, or `ADVANCE`, call the trusted automatic
   wrapper and then check status again:

   ```text
   Windows: cmd.exe /d /c "batch-review\review.cmd --auto"
   Linux/CentOS/macOS: ./batch-review/review.sh --auto
   ```

   This is an internal script call. Do not ask the operator to close and
   reopen a window between stages. If the status is `INITIALIZE`, `EDIT_CONFIG`,
   or `INSTALL_SCANNERS`, stop and report the one required manual setup action;
   never guess configuration or silently install software.

3. For `AI_REVIEW`, read only the small queue metadata file named by status.
   Prefer `ai-review-queue.json`; fall back to `ai-review-current.json` for an
   older single-item queue. Do not read a handoff in the parent conversation.
   For every queue item whose `expected_result` does not yet exist, start one
   fresh project subagent of type `skill-security-reviewer`. Send it only that
   item's `task_id`, `handoff`, and `expected_result`. The project subagent
   preloads `/skill-security-review`, enforces the read/write boundary, and
   returns only its completion metadata.

   Queue items are independent. Launch up to the queue's `max_parallel` items
   at a time (the value comes from `[concurrency].ai_reviews`). If the Agent
   interface serializes calls, process the same queue in bounded groups; never
   combine multiple Skills into one context or substitute a generic Agent.

4. After all expected results for the current queue are present, call the
   automatic wrapper once more. It validates and imports every ready result in
   the current repository, writes the per-Skill and batch results, cleans the
   temporary repository area only after the cleanup gate passes, and prepares
   the next repository. Check status again and repeat from step 2 or 3.

5. For `VIEW_RESULTS` or `COMPLETE`, report only the batch ID, result CSV/JSON
   paths, completed count, and non-passing or incomplete count. Do not open
   reports in the parent context.

If an Agent is unavailable, a result is malformed, a path is unexpected, a
launcher fails, or additional authority is requested, stop with a concise
`CONTEXT_ISOLATION_UNAVAILABLE` or launcher error. Do not perform the review
inline and do not skip a task. The persisted state and restricted evidence
make the loop resumable.
