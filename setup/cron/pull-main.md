# Cron Job: pull-main

Keeps workspace synced with origin/main. Script-driven for Git hygiene: `scripts/pull-main.sh` handles clean pulls + dirty-state recovery (GitHub issue creation + repo reset) without agent reasoning.

## Registration

```
CronCreate:
  schedule: "*/30 * * * *"
  durable: true
  name: "pull-main"
  sessionTarget: isolated
  model: litellm/claude-haiku-4-5
  payload:
    kind: agentTurn
  timeout-seconds: 300
```

Isolated Haiku maintenance session. Executes `scripts/pull-main.sh`, stays silent (`NO_REPLY`). Cron spec re-application after pulls handled by heartbeat drift correction (`docs/heartbeat-checks.md` Check 2b), not this job — isolated session can't reliably call CronList/CronUpdate.

## Prompt

```
Before running the script, record the current HEAD commit:

  BEFORE_HEAD=$(git rev-parse HEAD)

Run scripts/pull-main.sh.

If `.pull-dirty` exists afterward, reply with ONLY: NO_REPLY.

After a successful run, record the current HEAD commit again:

  AFTER_HEAD=$(git rev-parse HEAD)

If `BEFORE_HEAD` and `AFTER_HEAD` are the same, reply with ONLY: NO_REPLY.

If `HEAD` advanced during this invocation, reply with ONLY: NO_REPLY.
```

## Notes

- Script handles Git-state recovery: clean pulls silent, dirty pulls create GitHub issue (preserving local changes) then reset repo.
- If `gh` auth missing, falls back to `GITHUB_PAT` from `.env` via `GH_TOKEN`. If neither available, leaves `.pull-dirty` for HEARTBEAT backstop (section 5) to retry via `scripts/pull-main.sh --recover-only` after auth restored. Until then, heartbeat preserves signal + surfaces problem for operator.
- Cron jobs auto-expire after 7 days. Heartbeat (`docs/heartbeat-checks.md` Checks 2/2b) re-registers missing jobs + patches drift.
- Post-pull cron spec re-application handled by heartbeat within next 60-minute cycle. Delay acceptable — cron spec changes rare, not user-facing.
- 30-minute cadence trims queue pressure without materially changing repo freshness for this workspace.
- 5-minute timeout leaves enough room for slower isolated sessions to finish instead of stacking retries.
- GitHub issue preserves local changes for PR-based review before incorporation. Enforces design principle: structural/prompt changes go through external review.
