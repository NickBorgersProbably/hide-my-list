# Cron Job: pull-main

Keeps the local workspace in sync with origin/main. The cron run is script-driven for Git hygiene: `scripts/pull-main.sh` handles clean pulls plus dirty-state recovery (GitHub issue creation + repo reset) without task-specific agent reasoning.

## Registration

```
CronCreate:
  schedule: "*/10 * * * *"
  durable: true
  name: "pull-main"
  sessionTarget: isolated
  model: litellm/claude-haiku-4-5
  payload:
    kind: agentTurn
  timeout-seconds: 60
```

This job runs as an isolated Haiku session. It executes the pull script and reports status. Cron spec re-application after pulls is handled by the heartbeat drift correction (HEARTBEAT.md Check 2b), not by this job — an isolated session cannot reliably call CronList/CronUpdate.

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

- The script handles Git-state recovery: clean pulls stay silent, and dirty pulls create a GitHub issue (preserving local changes) before resetting the repo.
- If interactive `gh` auth is missing, the script falls back to `GITHUB_PAT` from the repo `.env` by exporting `GH_TOKEN`. If neither auth path is available, it leaves `.pull-dirty` in place for the HEARTBEAT backstop (section 5) to retry via `scripts/pull-main.sh --recover-only` after auth is restored. Until then, heartbeat only preserves the signal and surfaces the problem for operator attention.
- Cron jobs auto-expire after 7 days. HEARTBEAT.md remains the safety net: it re-registers missing jobs and patches any drift.
- Post-pull cron spec re-application is handled by heartbeat drift correction within its next 60-minute cycle. This delay is acceptable because cron spec changes are rare operational events, not user-facing.
- The GitHub issue preserves local changes for PR-based review before they're incorporated back into the system. This enforces the design principle that structural/prompt changes go through external review.
