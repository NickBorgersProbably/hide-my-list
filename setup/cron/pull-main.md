# Cron Job: pull-main

Keeps the local workspace in sync with origin/main. The normal cron run is still script-driven for Git hygiene: `scripts/pull-main.sh` handles clean pulls plus dirty-state recovery (GitHub issue creation + repo reset) without task-specific agent reasoning. After a clean pull that advances `HEAD`, this cron also re-applies any changed `setup/cron/` specs to the live durable jobs so prompt, schedule, and delivery fixes take effect immediately instead of waiting for the next heartbeat pass.

## Registration

```
CronCreate:
  schedule: "*/10 * * * *"
  durable: true
  name: "pull-main"
  sessionTarget: main
  payload:
    kind: systemEvent
  delivery:
    mode: none
  timeout-seconds: 120
```

This job injects a `systemEvent` into the main agent session. The script still handles the normal pull and recovery flow; the prompt adds a post-pull cron sync step, while heartbeat only retries stale recovery signals when needed.

## Prompt

```
Before running the script, record the current HEAD commit:

  BEFORE_HEAD=$(git rev-parse HEAD)

Run scripts/pull-main.sh.

If `.pull-dirty` exists afterward, reply with ONLY: NO_REPLY.

After a successful run, record the current HEAD commit again:

  AFTER_HEAD=$(git rev-parse HEAD)

If `BEFORE_HEAD` and `AFTER_HEAD` are the same, reply with ONLY: NO_REPLY.

If `HEAD` advanced during this invocation, inspect whether any cron spec files
changed in that pull:

  git diff "$BEFORE_HEAD" "$AFTER_HEAD" -- setup/cron/

If `setup/cron/reminder-check.md`, `setup/cron/reminder-delivery.md`, or
`setup/cron/pull-main.md` changed:
- Use CronList first and match live jobs by `name`.
- Read each changed spec file in `setup/cron/` and treat it as the canonical
  source for `schedule`, `prompt`, `sessionTarget` (if any), `payload.kind`,
  `delivery`, `model` (when pinned), and `timeout-seconds`.
- Before creating or patching `reminder-delivery`, verify the local OpenClaw
  config already defines `litellm/claude-haiku-4-5` by running
  `bash setup/migrate-openclaw-config.sh --check`.
- If that check fails, do NOT create or patch `reminder-delivery` yet. Report
  that the operator must run `bash setup/migrate-openclaw-config.sh` and
  restart the gateway first, then continue reapplying any other changed cron
  specs.
- Use CronUpdate on the matching live job ID to patch only those affected jobs.
- Preserve the intended contract from the spec:
  - `reminder-check`: `sessionTarget: main`, `payload.kind: systemEvent`,
    `delivery.mode: none`, `timeout-seconds: 120`
  - `reminder-delivery`: no `sessionTarget` (isolated), `payload.kind: agentTurn`,
    `model: litellm/claude-haiku-4-5`, `delivery.mode: best-effort-deliver`,
    `timeout-seconds: 120`
  - `pull-main`: `sessionTarget: main`, `payload.kind: systemEvent`,
    `delivery.mode: none`, `timeout-seconds: 120`
- After updating any affected live jobs, reply with ONLY: NO_REPLY.

If no `setup/cron/` files changed, reply with ONLY: NO_REPLY.
```

## Notes

- The script still handles Git-state recovery: clean pulls stay silent, and dirty pulls create a GitHub issue (preserving local changes) before resetting the repo. The cron prompt now adds a targeted silent post-pull cron reapply step when `setup/cron/` changed in the new commit range.
- If `gh` is not authenticated, the script leaves `.pull-dirty` in place for the HEARTBEAT backstop (section 5) to retry via `scripts/pull-main.sh --recover-only` after auth is restored. Until then, heartbeat only preserves the signal and surfaces the problem for operator attention.
- Cron jobs auto-expire after 7 days. HEARTBEAT.md remains the safety net: it re-registers missing jobs and patches any residual drift that was not corrected by the post-pull reapply step.
- The GitHub issue preserves local changes for PR-based review before they're incorporated back into the system. This enforces the design principle that structural/prompt changes go through external review.
