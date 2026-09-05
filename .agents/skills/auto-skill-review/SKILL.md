---
name: auto-skill-review
description: Run the complete prepared Skill security-review batch on Windows or Unix. Use trusted scripts for deterministic work and isolated reviewer subagents for Skill content review.
---

# Automatic Skill Review for Codex CLI

This Skill is the coordinator. It may read small control files, but it must not
inspect target Skill packages, static scanner reports, manifests, prior AI reports,
or batch evidence. Trusted scripts perform CSV validation, repository download,
extraction, static scanning, state changes, result merging, reporting, and cleanup.

Do not use Git, package managers, web access, MCP, or arbitrary shell commands in
this workflow. Do not execute reviewed content. The operator does not need to run
`review.cmd` separately once initialization and scanner installation are complete.

## Coordinator loop

1. From the repository root, get machine-readable status:

   ```text
   Windows: cmd.exe /d /c "batch-review\status.cmd --json"
   Linux/CentOS/macOS: ./batch-review/status.sh --json
   ```

2. For `PLAN`, `START`, or `ADVANCE`, run the trusted automatic launcher, then
   read status again:

   ```text
   Windows: cmd.exe /d /c "batch-review\review.cmd --auto"
   Linux/CentOS/macOS: ./batch-review/review.sh --auto
   ```

   For `INITIALIZE`, `EDIT_CONFIG`, or `INSTALL_SCANNERS`, stop and report the
   single required operator action. Never guess configuration or install software
   without the operator's confirmation.

3. For `AI_REVIEW`, read only the queue path reported by status. Prefer
   `ai-review-queue.json`; accept `ai-review-current.json` only for a compatible
   legacy queue. Do not read handoffs in the coordinator context.

   For each queue item whose `expected_result` is absent, delegate exactly one
   item to a fresh project subagent named `skill_security_reviewer`. That agent
   follows the `$skill-security-review` adapter and the canonical policy. Send only:

   - `task_id`
   - `handoff`
   - `expected_result`

   Start no more than `max_parallel` reviewers at once. Every Skill must use a
   separate context. Never combine multiple Skills in one reviewer, review inline,
   or substitute a generic subagent that has not loaded the canonical review Skill.

4. When the queue's expected result files exist, run the automatic launcher again.
   It validates and imports results, creates reports, applies the cleanup gate, and
   prepares the next repository. Repeat until status is `COMPLETE`.

5. For `VIEW_RESULTS` or `COMPLETE`, report only the batch ID, result CSV/JSON
   paths, completed count, and non-passing or incomplete count. Do not open report
   contents in the coordinator context.

If a reviewer cannot be isolated, a result is malformed, a path is unexpected, or
a launcher asks for additional authority, stop with `CONTEXT_ISOLATION_UNAVAILABLE`
or the launcher error. Persisted batch state makes a later retry resumable.
