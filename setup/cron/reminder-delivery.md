# Cron Job: reminder-delivery

Delivers due reminders to the user. Runs every 15 minutes (offset from `reminder-check`) via OpenClaw's durable cron, pinned to Haiku for cost efficiency.

## Registration

```
CronCreate:
  schedule: "2,17,32,47 * * * *"
  durable: true
  name: "reminder-delivery"
  model: litellm/claude-haiku-4-5
  payload:
    kind: agentTurn
  delivery:
    mode: best-effort-deliver
  timeout-seconds: 120
```

This job runs in an **isolated cron session** — it does not use `sessionTarget: main` and does not inherit the main session's conversation history. This is a deliberate security boundary: the main session may contain untrusted GitHub content, and reminder delivery has Notion credentials and speaks to the user, so isolating it keeps the trust model at [BC] (credentials + external actions, but no untrusted input).

Delivery uses `mode: best-effort-deliver` so the agent's response reaches the user through the best available surface. The `model` field pins this job to Haiku — routine empty checks cost near-zero tokens, and actual delivery (reading a small JSON file, composing a short message, updating Notion) is well within Haiku's capability.

## Prompt

```
Check whether .reminder-signal exists.

If .reminder-signal does NOT exist, reply with ONLY: NO_REPLY

If .reminder-signal exists, read it. It contains a JSON object with a
"reminders" array. For each reminder:

1. Determine delivery status from the current time vs the reminder's
   remind_at timestamp:
   - If current time is within 15 minutes of remind_at: status is "sent"
   - If current time is more than 15 minutes after remind_at: status is "missed"

2. Deliver the reminder to the user:
   - On-time (sent): casual delivery ("Hey, time to [task]")
   - Missed (>15 min late): acknowledge the delay without shame
     ("This was scheduled a bit ago — [task]")

3. Update the reminder's status in Notion:
   scripts/notion-cli.sh update-property PAGE_ID '{"properties":{"Reminder Status":{"select":{"name":"STATUS"}}}}'
   where STATUS is "sent" or "missed" from step 1.

   Do NOT update the main task Status to Completed. The reminder delivery
   notifies the user — task completion is a separate action the user takes.

4. After ALL reminders are delivered and their Notion statuses updated,
   delete .reminder-signal.

If delivery or the Notion update fails for any reminder:
- Do NOT delete `.reminder-signal` blindly.
- Rewrite `.reminder-signal` so it contains only reminders that were not fully
  delivered and updated.
- Any reminder already delivered and updated in Notion must be omitted from the
  rewritten file so retries are idempotent.
- If none of the reminders succeeded, leaving the original file in place is
  acceptable.
```

## Notes

- The `reminder-check` cron job (every 15 min) runs `scripts/check-reminders.sh`, which queries Notion and writes `.reminder-signal` when reminders are due. This delivery job runs 2 minutes after each check to pick up the signal file.
- Existing installs must add `claude-haiku-4-5` to `~/.openclaw/openclaw.json`
  before this job can be registered safely. Run `bash setup/migrate-openclaw-config.sh`
  and restart the gateway after the migration.
- Cron jobs auto-expire after 7 days. HEARTBEAT.md re-registers the job if missing and patches it back to this spec if the live registration drifts.
- Cron only fires when the REPL is idle. If the user is mid-conversation, delivery queues until the conversation pauses — better for ADHD focus.
- The signal file contains only task IDs, titles, and timestamps — no secrets. Status classification happens here at actual delivery time, not at polling time, so the user-facing framing always matches reality.
- Because this job is isolated (no `sessionTarget`), it does not carry forward any GitHub-derived content from the main session. This keeps the reminder flow at [BC] in the agent trust model — credentials and external actions, but no untrusted input.
