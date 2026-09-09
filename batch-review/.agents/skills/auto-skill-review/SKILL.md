---
name: auto-skill-review
description: Run a security-review batch with trusted completion-driven scheduling, bounded reviewer watchdogs, isolated Skill reviewers, and incremental Chinese report localization.
---

# Automatic Skill Review for Codex CLI

The parent only dispatches native reviewer/localizer subagents and invokes the
trusted project checkpoints documented below. Never read target packages, handoff
contents, localization source text, package-manifest.json, static reports, prior AI
reports, Translation Memory, or batch evidence in the parent context. Do not use
Git, package managers, network, MCP, or arbitrary shell commands. Never execute
reviewed content.

## Initial checkpoint

From this project root, call:

```text
Windows: cmd.exe /d /c "review.cmd --auto --json --ai-parallel 5"
Linux/CentOS/macOS: ./review.sh --auto --json --ai-parallel 5
```

For a new batch, the trusted script completes Static Preparation for every
repository, cleans repository workspaces, writes the INTERIM report, and creates
the batch-wide queue. A fresh checkpoint also recovers result files left by an
interrupted prior coordinator one-by-one; one malformed orphan does not prevent
other valid orphan results from being persisted.

The JSON response contains a `dispatch_session` and only the newly leased
`ai_dispatch.items` (at most `max_parallel`). Preserve the session token as opaque
control metadata. Do not inspect queue/state files yourself.

## Reviewer dispatch, bounded waits, and completion events

1. For every supplied item, start one fresh project subagent named
   `skill_security_reviewer`. It follows `$skill-security-review`. Send only
   `task_id`, `handoff`, and `expected_result`.
2. Never combine Skills, review inline, or pass repository/evidence metadata to
   the parent context. Track only task IDs, the opaque dispatch session, and native
   completion/failure metadata.
3. Keep all supplied reviewers running concurrently, up to `max_parallel`.
4. **Never wait for the whole reviewer set. Never use an unbounded wait-all.**
   Wait for any single native reviewer event for at most about 60 seconds. If a
   reviewer finishes earlier, process it immediately. If no reviewer emits an
   event in that bounded interval, run the watchdog tick below and then continue
   another bounded wait cycle.
5. As soon as one Reviewer finishes successfully, immediately call the trusted
   checkpoint with that one completion event:

```text
Windows: cmd.exe /d /c "review.cmd --auto --json --ai-parallel 5 --completed-task-id <TASK_ID> --dispatch-session <SESSION>"
Linux/CentOS/macOS: ./review.sh --auto --json --ai-parallel 5 --completed-task-id <TASK_ID> --dispatch-session <SESSION>
```

6. The trusted script validates and finalizes only that Task, refreshes the
   current HTML/CSV/JSON projection, releases exactly one lease, and returns at
   most one newly leased replacement. Start that replacement immediately while
   all other existing reviewers continue running.
7. Reuse the same `dispatch_session` for every event from that coordinator
   session. Never synthesize a session or send an event for a Task that was not
   leased in that session.

### Native reviewer failure/cancellation

If the native subagent runtime reports a reviewer as failed or cancelled, do not
wait for its lease timeout. Immediately call:

```text
python tools/review_watchdog.py fail --dispatch-session <SESSION> --task-id <TASK_ID> --ai-parallel 5 --reason "<NATIVE_FAILURE_SUMMARY>"
```

The trusted watchdog records that Skill as `AI_REVIEW_AGENT_FAILED`, produces an
INCOMPLETE final result for that Skill, releases its lease, refreshes the report,
and refills the free slot. Do not retry that exact Task automatically because the
old reviewer may still write late to its fixed expected-result path.

### Silent/hung reviewer watchdog

If no reviewer emits a completion/failure event during a bounded wait cycle, call:

```text
python tools/review_watchdog.py tick --dispatch-session <SESSION> --timeout-seconds 1200 --ai-parallel 5
```

A tick is lightweight when all leases are younger than the timeout. Once a lease
exceeds the timeout, the watchdog records only that Skill as `AI_REVIEW_TIMEOUT`,
marks its final result INCOMPLETE, releases the stale lease, and refills the slot.
Healthy reviewers continue running. If all five reviewers are hung, all five are
failed deterministically after the timeout and the Batch can still finish instead
of waiting forever.

The default operational timeout is 1200 seconds (20 minutes). Keep the 60-second
bounded coordinator wait cycle separate from this lease timeout: the 60-second
cycle exists only so the parent remains responsive and can run watchdog ticks.

8. Continue completion/failure/watchdog cycles until the trusted checkpoint returns
   `VIEW_RESULTS` / `COMPLETE`. Do not open the report yet; continue with localization.

## Incremental zh-CN report localization

1. Ask trusted program code for the next immutable report-safe job:

```text
python tools/localize_report.py prepare --current
```

2. If `status=COMPLETE`, localization is finished. Report only `batch_id` and the
   final result paths already returned by the review checkpoint.
3. If `status=READY`, start one fresh project subagent named `report_localizer`.
   It follows `$report-zh-localizer`. Send only the four values from
   `localization_dispatch`: `job_id`, `input_path`, `result_schema_path`, and
   `expected_result`. The parent must not read the job input.
4. When the localizer completes, call:

```text
python tools/localize_report.py import --current --job-id <JOB_ID>
```

5. The trusted importer validates Schema/job/key/hash, merges only valid text into
   Translation Memory, refreshes HTML/CSV/JSON, and returns the next immutable
   `localization_dispatch` when pending text remains. Dispatch that next job and
   repeat until `status=COMPLETE`.
6. A localization failure never changes canonical security/quality/overall
   decisions. Stop localization with the specific error; the report remains usable
   with English fallback.

If the coordinator itself restarts and no prior live AI session can be continued,
start again with the initial checkpoint **without** a session token. The trusted
script creates a new session and safely recovers orphan result files before
redispatching still-missing Tasks. Localization may always restart from
`localize_report.py prepare --current`; Translation Memory prevents already
translated keys from being re-dispatched.

On unavailable isolation, malformed results, unexpected paths, or additional
authority requirements, stop with the specific issue (use
`CONTEXT_ISOLATION_UNAVAILABLE` when applicable). Reviewer failures/timeouts must
be persisted through the trusted watchdog; never silently skip them or reinterpret
them as PASS.
