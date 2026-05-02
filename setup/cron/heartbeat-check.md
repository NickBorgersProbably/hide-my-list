# Built-in: Heartbeat

Heartbeat = built-in OpenClaw feature, not a cron job. Configured in `openclaw.json`.

## Configuration (in openclaw.json)

```json
"heartbeat": {
  "every": "120m",
  "model": "litellm/claude-haiku-4-5",  // decoupled from cheap tier — heartbeat needs reasoning for drift detection
  "lightContext": true,              // bootstrap = HEARTBEAT.md only
  "isolatedSession": true            // skip prior conversation transcript
}
```

Reminder delivery does not depend on `heartbeat.target`. Heartbeat Check 1 sends reminders explicitly with the OpenClaw `message` tool (`action: send`, `channel: signal`). `target` controls where generic non-`HEARTBEAT_OK` output routes; without it, defaults to `"none"`, silently discarded. Keep `heartbeat.target` unset or `"none"`. Ops alerts use explicit `message(..., channel: signal, target: OPS_ALERT_SIGNAL_NUMBER)` from `HEARTBEAT.md` — not generic heartbeat reply routing.

## Behavior

Every 2 hours, OpenClaw runs agent with `HEARTBEAT.md` as context. Agent performs checks:

1. Resolve reminder handoff path (`REMINDER_SIGNAL_FILE` when set, else `.reminder-signal`) and check for stranded handoffs. On successful delivery: atomically update `state.json.recent_outbound` (read-merge-prune-write via temp file + rename) per reminder before `complete-reminder`; if state write fails, halt delivery and surface ops alert without deleting handoff. Delete handoff file once after the full batch succeeds.
2. Verify cron jobs registered (re-register if missing)
3. Compare live cron jobs against `setup/cron/`, patch drift
4. Test Notion connectivity
5. Verify environment intact
6. Pull main if flagged

Heartbeat is decoupled from the cheap tier and uses Haiku because the May 2026 qwen2.5 incident false-positived on Check 2b cron drift detection. `lightContext: true` strips the main-session bootstrap (AGENTS.md, SOUL.md, etc.) from heartbeat context — heartbeat reads `docs/heartbeat-checks.md` on demand instead. `isolatedSession: true` skips replaying prior transcripts. Together they reduce heartbeat per-run context cost without changing behavior. Heartbeat also processes stranded handoff files; the 2-hour cadence is part of production reminder-latency tradeoff.

## Notes

- Heartbeat = safety net for missing canonical cron jobs and spec drift. Job gone missing for any reason → next heartbeat re-registers. Live job drifts from `CronCreate` block in `setup/cron/` (`name`, `durable`, `schedule`, `prompt`, `sessionTarget`, `model`, unexpected `to`, `payload.kind`, `payload.lightContext`, `timeout-seconds`) → next heartbeat patches to spec. `docs/heartbeat-checks.md` defines authoritative comparison contract (HEARTBEAT.md is a bootstrap stub that delegates to it).
- Also every-2-hours backstop for reminder delivery. Isolated `reminder-check` cron writes `.reminder-signal`; heartbeat Check 1 reads and delivers stranded reminders.
- Heartbeat managed by OpenClaw, does not expire.
