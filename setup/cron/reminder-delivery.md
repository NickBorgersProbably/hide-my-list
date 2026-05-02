# Cron Job Family: reminder-<page_id> (one-shot delivery)

Per-reminder one-shot OpenClaw cron registered at intake. Fires once at the wall-clock `Remind At`, delivers the reminder via Signal, atomically updates `state.json.recent_outbound`, marks the Notion row Completed, then self-deletes (`deleteAfterRun: true`).

The recurring `reminder-check` cron + `.reminder-signal` handoff path stays as a safety net for anything this primary path misses (`CronCreate` failure at intake, gateway data loss, jobs that fail to fire). See `setup/cron/reminder-check.md`.

## Why this exists

OpenClaw's `agent-runner-reminder-guard` post-processes every assistant reply that matches a reminder-commitment regex (`I'll remind you`, `I'll set a reminder`, etc.) and appends `"Note: I did not schedule a reminder in this turn, so this will not trigger automatically."` unless the same turn registered a cron job (`successfulCronAdds > 0`) or an enabled cron shares the current `sessionKey`.

Registering this one-shot cron at intake satisfies the first condition, suppressing the framework note. It also delivers reminders at exact wall-clock time instead of relying on the up-to-~135-min worst-case latency of the polling backstop.

## Registration

```
CronCreate:
  name: "reminder-<notion_page_id>"
  durable: true
  deleteAfterRun: true
  schedule:
    kind: "at"
    at: "<remind_at ISO 8601 with timezone>"
  sessionTarget: main
  model: litellm/claude-haiku-4-5  # decoupled from cheap tier — multi-step user-facing flow needs reasoning
  payload:
    kind: agentTurn
    lightContext: false  # bootstrap loaded — agent needs SOUL.md tone, AGENTS.md state.json conventions
    message: |
      <delivery prompt — see Prompt section below>
  timeout-seconds: 300
```

`sessionTarget: main` fires the delivery turn in the main agent session. `lightContext: false` keeps bootstrap loaded so SOUL.md tone guidance and AGENTS.md `recent_outbound` conventions are in scope. Haiku is the chosen model for this multi-step user-facing flow: the delivery turn verifies Notion state, sends the user-facing message, mutates `state.json`, and completes the reminder. The delivery turn sends to the user via the `message` tool.

`deleteAfterRun: true` causes OpenClaw to remove the job from the cron store after a successful run. The field defaults to `true` for `schedule.kind: "at"` jobs, but we set it explicitly for clarity.

Job naming uses the Notion page id so reschedule logic can target a specific job by name (`CronDelete name: reminder-<page_id>`) before re-registering.

## Prompt

```
SYSTEM REMINDER DELIVERY for Notion page <PAGE_ID>.

1. Run scripts/notion-cli.sh get-page <PAGE_ID> and parse the response.

2. If Status is already "Completed", or Reminder Status is not "pending":
   reply NO_REPLY and stop. Another path already handled this reminder.

3. Send via OpenClaw `message` tool (action: send, channel: signal):
   - "Hey, time to <title>"
   Wording stays shame-safe per SOUL.md — no guilt, no lateness framing,
   no "you forgot", no pressure.

4. Atomically update state.json.recent_outbound: read current state.json
   (initialize if missing), prune expired recent_outbound entries, merge:
     {
       "type": "reminder",
       "page_id": "<PAGE_ID>",
       "title": "<title>",
       "status": "sent",
       "sent_at": "<now ISO>",
       "awaiting_response": true,
       "expires_at": "<now+24h ISO>"
     }
   Preserve all other top-level fields (active_task, streak, conversation_state).
   Write via temp file + rename.

5. Run scripts/notion-cli.sh complete-reminder <PAGE_ID> sent.

6. Reply NO_REPLY.

If step 3 succeeds but step 4 or 5 fails: reply NO_REPLY, do NOT call
complete-reminder again. The .reminder-signal backstop will pick the row
up at the next 15-minute poll and re-deliver. Duplicate delivery is
acceptable; missed delivery is not.
```

## Reschedule rules

- **From `recent_outbound` (post-delivery reschedule):** the previous reminder's Notion row is already `Completed`, so its one-shot cron has already fired and self-deleted. Intake creates a new Notion row + new `reminder-<new_page_id>` one-shot. No `CronDelete` needed.
- **Pre-fire reschedule (rare; user changes mind before reminder fires):** intake calls `CronDelete name: reminder-<old_page_id>` first, then creates a new Notion row + new one-shot. Old Notion row gets `update-status ... Completed` so the polling backstop won't re-deliver it.

## Notes

- One-shot `reminder-*` jobs are NOT covered by the heartbeat drift / re-registration check (`docs/heartbeat-checks.md` Checks 2 / 2b). That check covers only the recurring canonical catalog (`reminder-check`, `pull-main`). One-shots self-delete after firing, so verifying their continued presence makes no sense.
- `validate-model-refs.sh` (`scripts/validate-model-refs.sh`) hardcodes its cheap-tier canonical-cron list to `reminder-check.md` and `pull-main.md`; this one-shot delivery spec is intentionally decoupled from `modelTiers.cheap`. Keep its concrete `model:` line aligned with the multi-step delivery contract above.
- `validate-spec-catalog.sh` checks only `docs/*.md` membership, so this `setup/cron/*.md` file does not need to be listed in `docs/index.md` or `DEV-AGENTS.md`.
