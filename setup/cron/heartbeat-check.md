# Built-in: Heartbeat

Heartbeat = built-in OpenClaw feature, not a cron job. Configured in `openclaw.json`.

## Configuration (in openclaw.json)

```json
"heartbeat": {
  "every": "60m",
  "model": "litellm/claude-sonnet-4-6"
}
```

Reminder delivery does not depend on `heartbeat.target`. Heartbeat Check 1 sends reminders explicitly with the OpenClaw `message` tool (`action: send`, `channel: signal`). `target` controls where generic non-`HEARTBEAT_OK` output routes; without it, defaults to `"none"`, silently discarded. Keep `heartbeat.target` unset or `"none"`. Ops alerts use explicit `message(..., channel: signal, target: "<resolved number>")` from `HEARTBEAT.md` after heartbeat resolves `OPS_ALERT_SIGNAL_NUMBER` from `.env` — not generic heartbeat reply routing.

## Behavior

Every 60 min, OpenClaw runs agent with `HEARTBEAT.md` as context. Agent performs checks:

1. Resolve reminder handoff path (`REMINDER_SIGNAL_FILE` when set, else `.reminder-signal`) and check for stranded handoffs
2. Verify cron jobs registered (re-register if expired)
3. Compare live cron jobs against `setup/cron/`, patch drift
4. Test Notion connectivity
5. Verify environment intact
6. Pull main if flagged

Uses lighter model (Sonnet) — routine operational checks. Heartbeat also processes stranded handoff files; hourly cadence is part of production reminder-latency tradeoff.

## Notes

- Heartbeat = safety net for cron expiry and spec drift. Auto-expired job → next heartbeat re-registers. Live job drifts from `CronCreate` block in `setup/cron/` (`name`, `durable`, `schedule`, `prompt`, `sessionTarget`, `model`, unexpected `to`, `payload.kind`, `timeout-seconds`) → next heartbeat patches to spec. `HEARTBEAT.md` defines authoritative comparison contract.
- Also hourly backstop for reminder delivery. Isolated `reminder-check` cron writes `.reminder-signal`; heartbeat Check 1 reads and delivers stranded reminders.
- Heartbeat managed by OpenClaw, does not expire.
