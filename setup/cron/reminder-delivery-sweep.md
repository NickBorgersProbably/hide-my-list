# Cron Job: reminder-delivery-sweep

Runs every 2 hours via OpenClaw's durable cron system. This is the narrow idle-session delivery backstop for reminder handoff files.

## Registration

```
CronCreate:
  schedule: "0 */2 * * *"
  durable: true
  name: "reminder-delivery-sweep"
  sessionTarget: isolated
  model: litellm/qwen2.5  # must match setup/model-tiers.json cheap
  payload:
    kind: agentTurn
    lightContext: true  # empty bootstrap — cron prompt is self-contained
  timeout-seconds: 600
```

Isolated cheap-tier session (see `setup/model-tiers.json`). Executes only Check 1 from `docs/heartbeat-checks.md`: validate any reminder handoff file, deliver stranded reminders via the explicit Signal `message` tool path, update `state.json.recent_outbound`, complete delivered reminders, and delete the handoff only after successful delivery.

This job keeps reminder fallback latency short without making the daily heartbeat own deeper operational work. Primary reminder delivery remains the per-reminder one-shot cron registered at intake (`setup/cron/reminder-delivery.md`).

## Prompt

```
You are the reminder delivery sweep cron for hide-my-list.

Read docs/heartbeat-checks.md and execute only Daily Heartbeat Check 1, Stranded Reminder Signal. Do not run cron registration, Notion connectivity, dirty-pull, janitor drift, environment, state, memory, or run-history checks.
```

## Notes

- `reminder-delivery-sweep` belongs to the canonical recurring cron catalog with `heartbeat`, `reminder-check`, `pull-main`, and `janitor`.
- This job exists only to keep the reminder safety-net path time-bounded while heartbeat cadence stays daily.
- It uses the same shame-safe delivery wording and state/Notion ordering as heartbeat Check 1.
