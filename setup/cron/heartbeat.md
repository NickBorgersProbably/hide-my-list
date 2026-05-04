# Cron Job: heartbeat

Runs daily via OpenClaw's durable cron system. This replaces the built-in OpenClaw heartbeat configured under `agents.defaults.heartbeat`; the template keeps that built-in path disabled with `every: "0s"`.

## Registration

```
CronCreate:
  schedule: "0 9 * * *"  # daily at 04:00 CT
  durable: true
  name: "heartbeat"
  sessionTarget: isolated
  model: litellm/qwen2.5  # must match setup/model-tiers.json cheap
  payload:
    kind: agentTurn
    lightContext: true  # empty bootstrap — cron prompt is self-contained
  timeout-seconds: 600
```

Isolated cheap-tier maintenance session (see `setup/model-tiers.json`). Executes the daily safety-net checks in `docs/heartbeat-checks.md`: stranded reminder delivery, canonical recurring cron registration existence, Notion connectivity, outbound media permission verification, and dirty-pull recovery. Deeper drift and environment audits are handled by the weekly `janitor` cron. The prompt is self-contained and does not depend on `HEARTBEAT.md` being loaded by OpenClaw's built-in heartbeat feature.

Reminder delivery does not depend on `heartbeat.target` or generic cron replies. Heartbeat Check 1 sends reminders explicitly with the OpenClaw `message` tool (`action: send`, `channel: signal`). Ops alerts also use explicit `message(..., channel: signal, target: OPS_ALERT_SIGNAL_NUMBER)` from `docs/heartbeat-checks.md`.

## Prompt

```
You are the scheduled heartbeat cron for hide-my-list.

Read docs/heartbeat-checks.md and execute the Daily Heartbeat Checks in order. Use that file as the authoritative contract for reminder handoff validation, cron registration existence repair, Notion connectivity, outbound media permission verification, dirty-pull recovery, and final output.
```

## Notes

- Built-in OpenClaw heartbeat is disabled in `setup/openclaw.json.template` (`agents.defaults.heartbeat.every: "0s"`) because `heartbeat.model` overrides have been less reliable across OpenClaw versions than cron `payload.model` overrides.
- `heartbeat` belongs to the canonical recurring cron catalog with `reminder-check`, `reminder-delivery-sweep`, `pull-main`, and `janitor`. Daily Check 2 in `docs/heartbeat-checks.md` verifies all five jobs exist. Weekly janitor Check 2b compares full live specs and patches drift.
- The job can re-register missing sibling jobs while it is running. If the `heartbeat` job is deleted entirely, the main agent startup flow re-registers it from this file on the next user interaction.
