# Built-in: Heartbeat

The heartbeat is not a cron job we create — it's a built-in OpenClaw feature configured in `openclaw.json`.

## Configuration (in openclaw.json)

```json
"heartbeat": {
  "every": "60m",
  "model": "litellm/claude-sonnet-4-6"
}
```

Reminder delivery does not depend on `heartbeat.target`. Heartbeat Check 1 sends reminders explicitly with the OpenClaw `message` tool (`action: send`, `channel: signal`). Operator-facing heartbeat failures also use explicit `message` sends, with `target: OPS_ALERT_SIGNAL_NUMBER` resolved from `.env`. The `target` field in the heartbeat config only controls where generic non-`HEARTBEAT_OK` heartbeat output is routed; keep it absent or set it to `"none"` so raw ops text is not dumped into the user thread.

## Behavior

Every 60 minutes, OpenClaw runs the agent with `HEARTBEAT.md` as context. The agent performs the checks defined in `HEARTBEAT.md`:

1. Resolve the reminder handoff path (`REMINDER_SIGNAL_FILE` when set, otherwise `.reminder-signal`) and check for stranded reminder handoffs there
2. Resolve `OPS_ALERT_SIGNAL_NUMBER` from `.env` for explicit operator alerting
3. Verify cron jobs are registered (re-register if expired)
4. Compare live cron jobs against `setup/cron/` and patch any drift
5. Test Notion connectivity
6. Verify environment is intact
7. Pull main if flagged

Uses a lighter model (Sonnet) since these are routine operational checks. Heartbeat also participates in reminder delivery by processing stranded handoff files, so its hourly cadence is part of the production reminder-latency tradeoff.

## Notes

- The heartbeat is the safety net for cron job expiry and spec drift. If a cron job auto-expires after 7 days, the next heartbeat re-registers it. If a live job's effective registration drifts from the `CronCreate` block in `setup/cron/` (for example `name`, `durable`, `schedule`, `prompt`, `sessionTarget`, `model`, an unexpected direct-delivery `to`, `payload.kind`, or `timeout-seconds`), the next heartbeat patches it back to the spec and alerts the separate operator recipient via `message(..., channel: signal, target: OPS_ALERT_SIGNAL_NUMBER)`. `HEARTBEAT.md` defines the authoritative comparison contract.
- The heartbeat is also the hourly backstop for reminder delivery. The isolated `reminder-check` cron only writes `.reminder-signal`; heartbeat Check 1 reads and delivers stranded reminders.
- Notion failures, malformed reminder handoffs, missing required env vars, and dirty-pull recovery problems follow the same explicit operator-alert route. Those messages are operational diagnostics, not user-facing reminder copy.
- The heartbeat itself is managed by OpenClaw and does not expire.
