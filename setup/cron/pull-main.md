# Cron Job: pull-main

Keeps the local workspace in sync with origin/main. The normal cron run is script-driven: `scripts/pull-main.sh` handles clean pulls plus dirty-state recovery (GitHub issue creation + repo reset) without task-specific agent reasoning. HEARTBEAT remains the retry backstop if `.pull-dirty` persists.

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

This job injects a `systemEvent` into the main agent session. The prompt is trivial because the script handles the normal pull and recovery flow; heartbeat only retries stale recovery signals when needed.

## Prompt

```
Run scripts/pull-main.sh.
Reply with ONLY: NO_REPLY
```

## Notes

- The script handles everything: clean pulls silently, dirty pulls by creating a GitHub issue (preserving local changes) and resetting the repo. The agent's only job is to run the script.
- If `gh` is not authenticated, the script leaves `.pull-dirty` in place for the HEARTBEAT backstop (section 5) to retry via `scripts/pull-main.sh --recover-only` after auth is restored. Until then, heartbeat only preserves the signal and surfaces the problem for operator attention.
- Cron jobs auto-expire after 7 days. HEARTBEAT.md re-registers the job if missing and patches it back to this spec if the live registration drifts.
- The GitHub issue preserves local changes for PR-based review before they're incorporated back into the system. This enforces the design principle that structural/prompt changes go through external review.
