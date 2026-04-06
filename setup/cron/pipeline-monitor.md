# Cron Job: pipeline-monitor

Replaces `scripts/monitor-pipeline.sh` and `scripts/webhook-signal.sh`. Runs every 2 minutes via OpenClaw's durable cron.

## Registration

```
CronCreate:
  schedule: "*/2 * * * *"
  durable: true
  name: "pipeline-monitor"
  sessionTarget: main
  payload:
    kind: systemEvent
  delivery:
    mode: none
  timeout-seconds: 120
```

This job injects a `systemEvent` into the main agent session instead of spawning an isolated cron-specific sub-agent. Delivery is `mode: none` because hide-my-list should only send a user-facing update when there is something actionable to say. The 120s timeout gives the LLM enough time to process the full agent context.

## Prompt

```
Run scripts/check-github-status.sh and compare with the last known state. If there are
new PR comments, review status changes, or workflow failures since the last check,
summarize the changes briefly. Focus on actionable items: new review comments that need
responses, failed CI that needs fixing, or PRs that are ready to merge.
If there is nothing actionable to report, reply with ONLY: NO_REPLY
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

- Cron jobs auto-expire after 7 days. HEARTBEAT.md re-registers the job if missing and patches it back to this spec if the live registration drifts.
- The workhorse script `check-github-status.sh` is unchanged.
- The old webhook approach (`webhook-signal.sh`) used socat to listen on a port, which was fragile in containers and added attack surface. The RemoteTrigger approach uses OpenClaw's native API.
