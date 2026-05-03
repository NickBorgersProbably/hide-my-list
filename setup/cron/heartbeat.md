# Cron Job: heartbeat

Runs every 2 hours via OpenClaw's durable cron system. This replaces the built-in OpenClaw heartbeat configured under `agents.defaults.heartbeat`; the template keeps that built-in path disabled with `every: "0s"`.

## Registration

```
CronCreate:
  schedule: "0 */2 * * *"
  durable: true
  name: "heartbeat"
  sessionTarget: isolated
  model: litellm/qwen2.5  # must match setup/model-tiers.json cheap
  payload:
    kind: agentTurn
    lightContext: true  # empty bootstrap — cron prompt is self-contained
  timeout-seconds: 600
```

Isolated cheap-tier maintenance session (see `setup/model-tiers.json`). Executes the operational checks in `docs/heartbeat-checks.md`: stranded reminder delivery, recurring cron registration and drift repair, Notion connectivity, environment checks, and dirty-pull recovery. The prompt is self-contained and does not depend on `HEARTBEAT.md` being loaded by OpenClaw's built-in heartbeat feature.

Reminder delivery does not depend on `heartbeat.target` or generic cron replies. Heartbeat Check 1 sends reminders explicitly with the OpenClaw `message` tool (`action: send`, `channel: signal`). Ops alerts also use explicit `message(..., channel: signal, target: OPS_ALERT_SIGNAL_NUMBER)` from `docs/heartbeat-checks.md`.

## Prompt

```
You are the scheduled heartbeat cron for hide-my-list.

Read docs/heartbeat-checks.md and execute the checks in order. Use that file as the authoritative contract for reminder handoff validation, cron registration repair, drift correction, Notion connectivity, environment checks, dirty-pull recovery, and final output.
```

## Notes

- Built-in OpenClaw heartbeat is disabled in `setup/openclaw.json.template` (`agents.defaults.heartbeat.every: "0s"`) because `heartbeat.model` overrides have been less reliable across OpenClaw versions than cron `payload.model` overrides.
- `heartbeat` belongs to the canonical recurring cron catalog with `reminder-check` and `pull-main`. Check 2/2b in `docs/heartbeat-checks.md` verifies all three jobs against their `setup/cron/` specs.
- The job can patch its own drift while it is running. If the `heartbeat` job is deleted entirely, re-register it manually from this file or run the main agent startup flow after setup.
