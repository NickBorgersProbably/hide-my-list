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

1. Resolve the reminder handoff path (`REMINDER_SIGNAL_FILE` when set, otherwise `.reminder-signal`) and check for stranded reminder handoffs there
2. Verify cron jobs are registered (re-register if expired)
3. Compare live cron jobs against `setup/cron/` and patch any drift
4. Test Notion connectivity
5. Verify environment is intact
6. Pull main if flagged

Uses a lighter model (Sonnet) since these are routine operational checks. Heartbeat is an infrastructure safety net, not the user-facing reminder clock, so hourly cadence is sufficient for production.

## Notes

- The heartbeat is the safety net for cron job expiry and spec drift. If a cron job auto-expires after 7 days, the next heartbeat re-registers it. If a live job's effective registration drifts from the `CronCreate` block in `setup/cron/` (for example `name`, `durable`, `schedule`, `prompt`, `sessionTarget`, an unexpected direct-delivery `to`, `payload.kind`, `best-effort-deliver`/`delivery.mode`, or `timeout-seconds`), the next heartbeat patches it back to the spec. `HEARTBEAT.md` defines the authoritative comparison contract.
- `pull-main` provides the fast path for cron spec changes: after a clean pull that advances `HEAD`, it immediately reapplies any changed `setup/cron/` specs from that pull's commit range. Heartbeat still catches anything the fast path missed.
- The heartbeat itself is managed by OpenClaw and does not expire.
