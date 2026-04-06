# Built-in: Heartbeat

The heartbeat is not a cron job we create — it's a built-in OpenClaw feature configured in `openclaw.json`.

## Configuration (in openclaw.json)

```json
"heartbeat": {
  "every": "60m",
  "model": "litellm/claude-sonnet-4-6"
}
```

## Behavior

Every 60 minutes, OpenClaw runs the agent with `HEARTBEAT.md` as context. The agent performs the checks defined in `HEARTBEAT.md`:

1. Verify cron jobs are registered (re-register if expired)
2. Compare live cron jobs against `setup/cron/` and patch any drift
3. Test Notion connectivity
4. Verify environment is intact
5. Pull main if flagged

Uses a lighter model (Sonnet) since these are routine operational checks. Reminder delivery does not depend on heartbeat; the isolated `reminder-check` cron delivers directly with `delivery.mode: announce`.

## Notes

- The heartbeat is the safety net for cron job expiry and spec drift. If a cron job auto-expires after 7 days, the next heartbeat re-registers it. If a live job's effective registration drifts from the `CronCreate` block in `setup/cron/` (for example `name`, `durable`, `schedule`, `prompt`, `sessionTarget`, `model`, reminder `delivery.mode`, `payload.kind`, or `timeout-seconds`), the next heartbeat patches it back to the spec. `HEARTBEAT.md` defines the authoritative comparison contract.
- The heartbeat itself is managed by OpenClaw and does not expire.
