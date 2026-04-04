# Cron Job: pipeline-monitor

Replaces `scripts/monitor-pipeline.sh` and `scripts/webhook-signal.sh`. Runs every 2 minutes via OpenClaw's durable cron.

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

## RemoteTrigger (optional)

For on-demand notifications from GitHub Actions (replaces the socat webhook):

```
RemoteTrigger:
  name: "pr-notification"
  prompt: "A GitHub Actions workflow just completed. Run scripts/check-github-status.sh
           and report any actionable changes."
```

The GitHub Actions workflow can call the RemoteTrigger API endpoint instead of the old socat webhook.

## Notes

- Cron jobs auto-expire after 7 days. HEARTBEAT.md re-registers if missing.
- The workhorse script `check-github-status.sh` is unchanged.
- The old webhook approach (`webhook-signal.sh`) used socat to listen on a port, which was fragile in containers and added attack surface. The RemoteTrigger approach uses OpenClaw's native API.
