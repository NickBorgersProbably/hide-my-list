# Cron Job: pull-main

Keeps the local workspace in sync with origin/main. The script handles clean pulls silently — only signals the agent when something goes wrong.

## Registration

```
CronCreate:
  schedule: "*/10 * * * *"
  durable: true
  name: "pull-main"
  best-effort-deliver: true
  to: $SIGNAL_OWNER_NUMBER
  timeout-seconds: 120
```

`$SIGNAL_OWNER_NUMBER` comes from `.env`. The `best-effort-deliver` flag prevents Signal delivery failures from marking the job as errored. The 120s timeout gives the LLM enough time to process the full agent context.

## Prompt

```
Run scripts/pull-main.sh to pull origin/main.

If .pull-dirty exists afterward, the pull failed due to local tracked-file changes
or a merge conflict. Handle it:

1. Read .pull-dirty for details about what files are dirty and why
2. Check memory/ for any notes about why these changes were made
3. Create a GitHub issue on NickBorgersProbably/hide-my-list titled
   "Agent local changes need review: [brief description of what changed]" with:
   - Which tracked files were modified locally
   - The full diff content so nothing is lost
   - Any context from memory about why these changes were made
   - Current HEAD vs remote HEAD
4. After the issue is created, reset to match remote:
   git checkout -- .
   git clean -fd
   git pull origin main
5. Delete .pull-dirty after successful recovery
6. Briefly note what happened — do not alarm the user unless the reset also fails

If .pull-dirty does not exist, the pull was clean. No action needed.
```

## Notes

- Clean pulls (the common case) need zero agent involvement — the script handles them directly.
- Cron jobs auto-expire after 7 days. HEARTBEAT.md re-registers if missing.
- The GitHub issue preserves local changes for PR-based review before they're incorporated back into the system. This enforces the design principle that structural/prompt changes go through external review.
