# Built-in: Heartbeat

The heartbeat is not a cron job we create — it's a built-in OpenClaw feature configured in `openclaw.json`.

## Configuration (in `openclaw.json` under `agents.defaults.heartbeat`)

```json
"agents": {
  "defaults": {
    "heartbeat": {
      "every": "60m",
      "model": "litellm/claude-sonnet-4-6",
      "target": "signal"
    }
  }
}
```

The `target` field is required for reminder delivery. Without it, OpenClaw defaults to `"none"` and silently discards all heartbeat output — including reminders. Set it to `"signal"` (or whichever channel your user communicates on) so that non-`HEARTBEAT_OK` output is routed to the user.

## Behavior

Every 60 minutes, OpenClaw runs the agent with `HEARTBEAT.md` as context. The agent performs the checks defined in `HEARTBEAT.md`:

1. Resolve the reminder handoff path (`REMINDER_SIGNAL_FILE` when set, otherwise `.reminder-signal`) and check for stranded reminder handoffs there
2. Verify cron jobs are registered (re-register if expired)
3. Compare live cron jobs against `setup/cron/` and patch any drift
4. If `.config-drift` exists, compare the live OpenClaw config against `setup/openclaw.json.template` for the allowlisted behavior fields and patch drift via `config.patch`
5. Test Notion connectivity
6. Verify environment is intact
7. Retry stale dirty-pull recovery if `.pull-dirty` is still present

Uses a lighter model (Sonnet) since these are routine operational checks. Heartbeat also participates in reminder delivery by processing stranded handoff files, so its hourly cadence is part of the production reminder-latency tradeoff.

## Notes

- The heartbeat is the safety net for cron job expiry and spec drift. If a cron job auto-expires after 7 days, the next heartbeat re-registers it. If a live job's effective registration drifts from the `CronCreate` block in `setup/cron/` (for example `name`, `durable`, `schedule`, `prompt`, `sessionTarget`, `model`, an unexpected direct-delivery `to`, `payload.kind`, or `timeout-seconds`), the next heartbeat patches it back to the spec. `HEARTBEAT.md` defines the authoritative comparison contract.
- The heartbeat is also the backstop for allowlisted OpenClaw config drift after a template-changing pull. `scripts/pull-main.sh` writes `.config-drift` when `setup/openclaw.json.template` changes, and the next heartbeat compares only the syncable behavior fields (`agents.defaults.heartbeat`, `messages`, `commands`, `session`, and optional `channels.signal.defaultTo`) before patching them with `config.patch`.
- The heartbeat is also the hourly backstop for reminder delivery. The isolated `reminder-check` cron only writes `.reminder-signal`; heartbeat Check 1 reads and delivers stranded reminders.
- The heartbeat itself is managed by OpenClaw and does not expire.
