# Cron Job Family: reminder-<page_id> (one-shot delivery)

Per-reminder one-shot OpenClaw cron registered at intake. Fires once at the wall-clock `Remind At`, delivers the reminder via Signal, atomically updates `state.json.recent_outbound`, marks the Notion row Completed, then self-deletes (`deleteAfterRun: true`).

The recurring `reminder-check` cron + `.reminder-signal` handoff path stays as a safety net for anything this primary path misses (`CronCreate` failure at intake, gateway data loss, jobs that fail to fire). See `setup/cron/reminder-check.md`.

## Why this exists

OpenClaw's `agent-runner-reminder-guard` post-processes every assistant reply that matches a reminder-commitment regex (`I'll remind you`, `I'll set a reminder`, etc.) and appends `"Note: I did not schedule a reminder in this turn, so this will not trigger automatically."` unless the same turn registered a cron job (`successfulCronAdds > 0`) or an enabled cron shares the current `sessionKey`.

Registering this one-shot cron at intake satisfies the first condition, suppressing the framework note. It also delivers reminders at exact wall-clock time instead of relying on the up-to-~75-min worst-case latency of the polling backstop.

## Registration

```
CronCreate:
  name: "reminder-<notion_page_id>"
  durable: true
  deleteAfterRun: true
  schedule:
    kind: "at"
    at: "<remind_at ISO 8601 with timezone>"
  sessionTarget: isolated
  model: litellm/gemma4-small  # must match modelTiers.cheap
  payload:
    kind: agentTurn
    lightContext: false  # bootstrap loaded — agent needs SOUL.md tone, AGENTS.md state.json conventions
    timeoutSeconds: 300
    message: |
      <delivery prompt — see Prompt section below>
```

`sessionTarget: isolated` runs the delivery turn as an isolated session. `lightContext: false` keeps bootstrap loaded — the cheap model needs SOUL.md tone guidance and AGENTS.md `recent_outbound` schema without re-stating them inline. Model stays on `modelTiers.cheap` — delivery prompt is highly structured and well within cheap-tier reach. `delivery.mode: none` means the cron turn's own `message` tool call is the only user-facing output.

`deleteAfterRun: true` causes OpenClaw to remove the job from the cron store after a successful run. The field defaults to `true` for `schedule.kind: "at"` jobs, but we set it explicitly for clarity.

Job naming uses the Notion page id so reschedule logic can target a specific job by name (`CronDelete name: reminder-<page_id>`) before re-registering.

## Prompt

```
SYSTEM REMINDER DELIVERY for Notion page <PAGE_ID>.

1. Run scripts/notion-cli.sh get-page <PAGE_ID> and parse the response.

2. If Status is already "Completed", or Reminder Status is not "pending":
   reply NO_REPLY and stop. Another path already handled this reminder.

3. Compute lateness: now - Remind At. If lateness > 15 minutes, treat as missed.

4. Send via OpenClaw `message` tool (action: send, channel: signal):
   - On-time:  "Hey, time to <title>"
   - Missed:   "This was due a bit ago — <title>. Want to handle it now or reschedule?"
   Wording stays shame-safe per SOUL.md — no guilt, no "you forgot", no pressure.

5. Atomically update state.json.recent_outbound: read current state.json
   (initialize if missing), prune expired recent_outbound entries, merge:
     {
       "type": "reminder",
       "page_id": "<PAGE_ID>",
       "title": "<title>",
       "status": "<sent|missed>",
       "sent_at": "<now ISO>",
       "awaiting_response": true,
       "expires_at": "<now+24h ISO>"
     }
   Preserve all other top-level fields (active_task, streak, conversation_state).
   Write via temp file + rename.

6. Run scripts/notion-cli.sh complete-reminder <PAGE_ID> sent|missed
   (status matches what was delivered).

7. Reply NO_REPLY.

If step 4 succeeds but step 5 or 6 fails: reply NO_REPLY, do NOT call
complete-reminder again. The .reminder-signal backstop will pick the row
up at the next 15-minute poll and re-deliver. Duplicate delivery is
acceptable; missed delivery is not.
```

## Reschedule rules

- **From `recent_outbound` (post-delivery reschedule):** the previous reminder's Notion row is already `Completed`, so its one-shot cron has already fired and self-deleted. Intake creates a new Notion row + new `reminder-<new_page_id>` one-shot. No `CronDelete` needed.
- **Pre-fire reschedule (rare; user changes mind before reminder fires):** intake calls `CronDelete name: reminder-<old_page_id>` first, then creates a new Notion row + new one-shot. Old Notion row gets `update-status ... Completed` so the polling backstop won't re-deliver it.

## Notes

- One-shot `reminder-*` jobs are NOT covered by the heartbeat drift / re-registration check (`docs/heartbeat-checks.md` Checks 2 / 2b). That check covers only the recurring canonical catalog (`reminder-check`, `pull-main`). One-shots self-delete after firing, so verifying their continued presence makes no sense.
- `validate-model-refs.sh` (`scripts/validate-model-refs.sh`) hardcodes its canonical-cron list to `reminder-check.md` and `pull-main.md`, so this spec file is intentionally out of scope for tier validation. Keep `model: litellm/<modelTiers.cheap>` here as a contract anyway — drift here is invisible to CI but still operationally meaningful.
- `validate-spec-catalog.sh` checks only `docs/*.md` membership, so this `setup/cron/*.md` file does not need to be listed in `docs/index.md` or `DEV-AGENTS.md`.
