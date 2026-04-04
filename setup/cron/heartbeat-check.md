# Built-in: Heartbeat

The heartbeat is not a cron job we create — it's a built-in OpenClaw feature configured in `openclaw.json`.

## Configuration (in openclaw.json)

```json
"heartbeat": {
  "every": "30m",
  "model": "litellm/claude-sonnet-4-6"
}
```

## Behavior

Every 30 minutes, OpenClaw runs the agent with `HEARTBEAT.md` as context. The agent performs the checks defined in `HEARTBEAT.md`:

1. Check for stranded `.reminder-signal` reminder handoffs
2. Verify cron jobs are registered (re-register if expired)
3. Test Notion connectivity
4. Verify environment is intact
5. Pull main if flagged

Uses a lighter model (Sonnet) since these are routine operational checks.

## Notes

- The heartbeat is the safety net for cron job expiry. If a cron job auto-expires after 7 days, the next heartbeat re-registers it.
- The heartbeat itself is managed by OpenClaw and does not expire.
