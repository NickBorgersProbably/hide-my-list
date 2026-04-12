# Built-in: Heartbeat

The heartbeat is not a cron job we create — it's a built-in OpenClaw feature configured in `openclaw.json`.

## Configuration (in openclaw.json)

```json
"heartbeat": {
  "every": "60m",
  "model": "litellm/claude-sonnet-4-6",
  "target": "signal"
}
```

Reminder delivery does not depend on `heartbeat.target`. Heartbeat Check 1 sends reminders explicitly with the OpenClaw `message` tool (`action: send`, `channel: signal`). The `target` field only controls where generic non-`HEARTBEAT_OK` heartbeat output is routed; without it, that generic heartbeat text defaults to `"none"` and is silently discarded. The production template sets `heartbeat.target` to `"signal"` so operator-facing heartbeat alerts reach the user there too.

## Behavior

Every 60 minutes, OpenClaw runs the agent with `HEARTBEAT.md` as context. The agent performs the checks defined in `HEARTBEAT.md`:

1. Resolve the reminder handoff path (`REMINDER_SIGNAL_FILE` when set, otherwise `.reminder-signal`) and check for stranded reminder handoffs there
2. Verify cron jobs are registered (re-register if expired)
3. Compare live cron jobs against `setup/cron/` and patch any drift
4. Test Notion connectivity
5. Verify environment is intact
6. Pull main if flagged

Uses a lighter model (Sonnet) since these are routine operational checks. Heartbeat also participates in reminder delivery by processing stranded handoff files, so its hourly cadence is part of the production reminder-latency tradeoff.

## Notes

- The heartbeat is the safety net for cron job expiry and spec drift. If a cron job auto-expires after 7 days, the next heartbeat re-registers it. If a live job's effective registration drifts from the `CronCreate` block in `setup/cron/` (for example `name`, `durable`, `schedule`, `prompt`, `sessionTarget`, `model`, an unexpected direct-delivery `to`, `payload.kind`, or `timeout-seconds`), the next heartbeat patches it back to the spec. `HEARTBEAT.md` defines the authoritative comparison contract.
- The heartbeat is also the hourly backstop for reminder delivery. The isolated `reminder-check` cron only writes `.reminder-signal`; heartbeat Check 1 reads and delivers stranded reminders.
- The heartbeat itself is managed by OpenClaw and does not expire.
