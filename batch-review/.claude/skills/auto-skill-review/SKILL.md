---
name: auto-skill-review
description: Run a security-review batch with trusted completion-driven scheduling, isolated reviewers, and incremental Chinese report localization.
allowed-tools: Bash Agent
---

# Automatic Skill Review for Claude Code

The parent only dispatches native reviewer/localizer Agents and invokes the
trusted project checkpoints documented below. Never read target packages, handoff
contents, localization source text, package-manifest.json, static reports, prior AI
reports, Translation Memory, or batch evidence in the parent context. Do not use Git, package managers, network, MCP, or arbitrary shell commands. Never execute
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

## Reviewer dispatch and completion events

1. For every supplied item, start one fresh project Agent of type
   `skill-security-reviewer`. It preloads `/skill-security-review`. Send only
   `task_id`, `handoff`, and `expected_result`.
2. Never combine Skills, review inline, or pass repository/evidence metadata to
   the parent context. Track only task IDs, the opaque dispatch session, and native
   completion metadata.
3. Keep all supplied reviewers running concurrently, up to `max_parallel`.
4. **As soon as one Reviewer finishes**, do not wait for the others. Immediately
   call the trusted checkpoint with that one completion event:

```text
Windows: cmd.exe /d /c "review.cmd --auto --json --ai-parallel 5 --completed-task-id <TASK_ID> --dispatch-session <SESSION>"
Linux/CentOS/macOS: ./review.sh --auto --json --ai-parallel 5 --completed-task-id <TASK_ID> --dispatch-session <SESSION>
```

5. The trusted script validates and finalizes only that Task, refreshes the
   current HTML/CSV/JSON projection, releases exactly one lease, and returns at
   most one newly leased replacement. Start that replacement immediately while
   all other existing reviewers continue running.
6. Reuse the same `dispatch_session` for every completion from that coordinator
   session. Never synthesize a session or send a completion for a Task that was
   not leased in that session.
7. If one completion returns `exit_code != 0`, report that Task and `log_path`.
   Do **not** cancel other already-running reviewers and do not refill the failed
   Task's slot. Continue processing completion events from the remaining leased
   reviewers; the failed result stays diagnosable and recoverable.
8. Continue until the checkpoint returns `VIEW_RESULTS` / `COMPLETE`. Do not open
   the report yet; continue with localization.

## Incremental zh-CN report localization

1. Ask trusted program code for the next immutable report-safe job:

```text
python tools/localize_report.py prepare --current
```

2. If `status=COMPLETE`, localization is finished. Report only `batch_id` and the
   final result paths already returned by the review checkpoint.
3. If `status=READY`, start one fresh project Agent of type `report-localizer`.
   It preloads `/report-zh-localizer`. Send only `job_id`, `input_path`,
   `result_schema_path`, and `expected_result` from `localization_dispatch`.
   The parent must not read the job input.
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

On unavailable isolation, agent failure, malformed results, unexpected paths, or
additional authority requirements, stop with the specific issue (use
`CONTEXT_ISOLATION_UNAVAILABLE` when applicable). Do not skip failed review tasks.
