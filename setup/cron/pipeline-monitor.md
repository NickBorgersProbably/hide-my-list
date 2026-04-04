# Cron Job: pipeline-monitor

Replaces `scripts/monitor-pipeline.sh`. Runs every 2 minutes via OpenClaw's durable cron and serves as the fallback path when the lightweight GitHub callback signal is disabled or missed.

## Registration

```
CronCreate:
  schedule: "*/2 * * * *"
  durable: true
  name: "pipeline-monitor"
```

## Prompt

```
Run scripts/check-github-status.sh and compare with the last known state. If there are
new PR comments, review status changes, or workflow failures since the last check,
summarize the changes briefly. Focus on actionable items: new review comments that need
responses, failed CI that needs fixing, or PRs that are ready to merge.
```

## Fast-Path Callback

GitHub Actions currently supports an optional fast-path callback through `AGENT_WEBHOOK_URL`, configured as the repository variable `vars.AGENT_WEBHOOK_URL`:

```
Workflow step:
  name: Notify agent webhook
  behavior: Send a best-effort request to AGENT_WEBHOOK_URL after reviews complete
```

That callback is intentionally minimal: the listener ignores request data and only wakes the agent up sooner. The cron job remains the source of truth for eventual delivery if the callback fails.

## RemoteTrigger (future option)

OpenClaw `RemoteTrigger` could replace the current lightweight callback in the future:

```
RemoteTrigger:
  name: "pr-notification"
  prompt: "A GitHub Actions workflow just completed. Run scripts/check-github-status.sh
           and report any actionable changes."
```

## Notes

- Cron jobs auto-expire after 7 days. HEARTBEAT.md re-registers if missing.
- The workhorse script `check-github-status.sh` is unchanged.
- The callback path is optional acceleration, not the only notification mechanism.
- The old always-on webhook-only approach was fragile in containers; the cron monitor now provides the reliable baseline.
