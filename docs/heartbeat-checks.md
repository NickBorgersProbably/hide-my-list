# Heartbeat Checks

## Time and Timezone

Daily heartbeat runs as the durable cron job `heartbeat` with `payload.lightContext: true` — `USER.md` and `AGENTS.md` are NOT in bootstrap. Weekly janitor runs as the durable cron job `janitor` with `payload.lightContext: false`, so it has normal bootstrap context. The system clock is UTC. Do not derive user-local calendar context from system clock alone.

Before any check that formats a date for the user, or needs to convert a stored UTC instant such as `Remind At` into user-local calendar language, run:

```bash
scripts/user-time-context.sh [reference_timestamp]
```

Pass `[reference_timestamp]` when converting a specific instant such as a reminder's `remind_at` value into user-local phrasing like "today", "tomorrow", day-of-week names, or "at 9am". Call the helper with no argument when only the current user-local calendar context is needed.

The script returns JSON with `user_timezone`, `reference_utc`, `reference_local`, `local_date`, `local_day_of_week`, `tomorrow_date`, `tomorrow_day_of_week`. Use those fields — never compute "today" or "tomorrow" from `date(1)` directly.

Check 1 safety-net reminder status is produced upstream by `scripts/check-reminders.sh`: it compares UTC instants, determines `status: sent|missed`, and writes that status into the handoff file. Heartbeat consumes that handoff; it does not recompute `now - Remind At`. Use the helper only when delivery wording needs user-local phrasing derived from `remind_at` or from the current local date.

Required here because the daily heartbeat cron does not load `USER.md` or `AGENTS.md`, so this file is the only place the timezone contract lives for heartbeat-driven work — same pattern as the tone contract in Check 1. Janitor should use the same helper when it needs user-local date language so both cron paths share one timezone contract.

## Daily Heartbeat Checks (in order)

Before any check that may send an ops alert, resolve heartbeat ops-alert Signal recipient from `.env`:

```bash
OPS_ALERT_TARGET="$(bash -lc 'SCRIPT_DIR=$(cd "$(dirname scripts/check-reminders.sh)" && pwd); source "$SCRIPT_DIR/load-env.sh" OPS_ALERT_SIGNAL_NUMBER?; if [ -z "${OPS_ALERT_SIGNAL_NUMBER:-}" ]; then echo "missing OPS_ALERT_SIGNAL_NUMBER" >&2; exit 1; fi; printf "%s\n" "$OPS_ALERT_SIGNAL_NUMBER"')"
```

### 1. Stranded Reminder Signal
- Resolve reminder handoff file using same helper + repo-root filename validation scripts use, so `.env` overrides honored:
  ```bash
  HANDOFF_FILE="$(bash -lc 'SCRIPT_DIR=$(cd "$(dirname scripts/check-reminders.sh)" && pwd); ROOT_DIR=$(cd "$SCRIPT_DIR/.." && pwd); source "$SCRIPT_DIR/load-env.sh" REMINDER_SIGNAL_FILE?; SIGNAL_BASENAME=${REMINDER_SIGNAL_FILE:-.reminder-signal}; case "$SIGNAL_BASENAME" in ""|"."|".."|*/*) echo "invalid REMINDER_SIGNAL_FILE" >&2; exit 1 ;; esac; printf "%s\n" "$ROOT_DIR/$SIGNAL_BASENAME"')"
  ```
- File = reminder handoff written by `scripts/check-reminders.sh`
- If `HANDOFF_FILE` exists at heartbeat time: treat as undelivered. Read + validate (must be JSON with `reminders` array; each entry needs string `page_id`, non-empty string `title`, and string `status`. New handoff writers emit only `sent`; legacy `missed` entries are still valid and should be normalized to `sent` after delivery. Any other shape/status = malformed → leave file, send ops alert via `message` tool (`action: send`, `channel: signal`, `target: "$OPS_ALERT_TARGET"`) describing the malformed handoff, skip delivery/`complete-reminder`/delete). For each valid reminder:
  - Send the same shame-safe wording every time: `Hey, time to [task]`.
  - No guilt, criticism, lateness commentary, "you forgot", "you should have", or pressure framing. Required here because heartbeat runs with `lightContext: true` → no AGENTS.md in bootstrap, so this file is the only place the tone contract lives for heartbeat-delivered reminders.
  - After successful send, atomically update `state.json.recent_outbound`: read current `state.json` (initialize if missing), prune expired `recent_outbound` entries, merge the new reminder entry (`type: "reminder"`, `page_id`, `title`, `status: "sent"`, `sent_at`, `awaiting_response: true`, `expires_at` about 24h later) while preserving all other fields (`active_task`, streak, conversation state), write via temp file + rename. If this state write fails, do not run `complete-reminder` or delete the handoff file — surface an ops alert (same channel/recipient as malformed-handoff alert above) and leave handoff for explicit recovery.
  - Then run `scripts/notion-cli.sh complete-reminder PAGE_ID sent`.
- After all valid reminders processed: delete handoff file once.
- Daily delivery safety net. Primary reminder delivery is the per-reminder one-shot cron registered at intake (`setup/cron/reminder-delivery.md`); this Check 1 + AGENTS.md startup check catch anything the one-shot fails to deliver.

Why the `state.json` write matters: once reminder delivery succeeds, the handoff file is correctly deleted and Notion reminder record is already completed. Without `recent_outbound`, the next session loses the only bridge that makes a reply like "I did it" or "reschedule for tomorrow" interpretable.

### 2. Cron Job Health
Verify durable canonical recurring cron jobs are registered. Re-register any missing from the canonical `setup/cron/` specs. This is an existence check only; full drift comparison is weekly janitor Check 2b.

| Job | Schedule | Action |
|-----|----------|--------|
| heartbeat | `0 9 * * *` | Read this file and execute the Daily Heartbeat Checks |
| reminder-check | `*/15 * * * *` | Run `scripts/check-reminders.sh` (query-only; writes reminder handoff if reminders due) |
| pull-main | `*/10 * * * *` | Run `scripts/pull-main.sh`; script handles dirty-pull recovery |
| janitor | `0 7 * * 1` | Read this file and execute the Weekly Janitor Checks |

Check via CronList. Missing → re-create with CronCreate (durable: true) using schedule, prompt, options from `setup/cron/`. Canonical fields come from `setup/cron/<name>.md`: `sessionTarget`, exact `model:` line, `payload.kind`, `payload.lightContext`, and `timeout-seconds`. `heartbeat`, `reminder-check`, and `pull-main` must match `modelTiers.cheap` in `setup/openclaw.json.template`; `janitor` is decoupled from cheap tier and uses its explicit Opus model for weekly deep audits. Cron jobs never use direct-delivery routing; heartbeat Check 1 uses explicit `message` tool calls for reminder delivery and ops alerts.

**Scope:** this check covers only the recurring canonical jobs above. Per-reminder one-shot `reminder-<page_id>` jobs (registered at intake per `setup/cron/reminder-delivery.md`) are NOT verified or re-registered here — they self-delete after firing, so checking for their presence makes no sense. If the `heartbeat` cron is deleted entirely, AGENTS.md startup checks re-register it on the next user interaction; while heartbeat is running, it self-heals missing sibling jobs.

### 3. Notion Connectivity
- Run `scripts/notion-cli.sh query-pending` with short timeout
- Fails → send ops alert via `message` tool (`action: send`, `channel: signal`, `target: "$OPS_ALERT_TARGET"`) with error detail, no aggressive retry

### 5. Dirty Pull Recovery (safety net)
- `.pull-dirty` exists + older than 20 min → pull-main cron may have failed recovery
- Run `scripts/pull-main.sh --recover-only` after fixing underlying problem (restore interactive `gh` auth, export valid `GH_TOKEN`, or provide `GITHUB_PAT` in repo `.env` — helper exports as `GH_TOKEN`). Script creates GitHub issue + resets repo when recovery can proceed.
- Recovery still fails → send ops alert via `message` tool (`action: send`, `channel: signal`, `target: "$OPS_ALERT_TARGET"`) describing the failure
- Normally pull-main cron handles recovery. This backstop for cases where GitHub auth was unavailable or script errored; until `gh` auth, valid `GH_TOKEN`, or `GITHUB_PAT` available, heartbeat preserves `.pull-dirty` + surfaces problem

Nothing needs attention → reply HEARTBEAT_OK.

## Weekly Janitor Checks (in order)

### 2b. Cron Spec Drift Check
For each registered canonical recurring cron job (`heartbeat`, `reminder-check`, `pull-main`, `janitor`), compare live registration against canonical `CronCreate` spec in `setup/cron/<name>.md`.

Check: CronList for live registrations, read spec files in `setup/cron/`.

Compare + correct these fields:
- `name`
- `durable`
- `schedule`
- `prompt`
- `sessionTarget` (canonical: `isolated` for all four recurring jobs)
- `model` (canonical: exact `model:` line in `setup/cron/<name>.md`; cron specs must keep that value aligned with `modelTiers.cheap` in `setup/openclaw.json.template`)
- direct-delivery routing: live `to` if present (should not exist)
- payload: canonical `payload.kind` + `payload.lightContext` from the spec
- `timeout-seconds`

Stale `pipeline-monitor` cron still registered → delete with CronDelete (job removed).

Field differs from spec → patch with CronUpdate. Identity field (`name`, `durable`) can't be safely changed → delete + re-create from spec. Intended contract:
- `heartbeat`: `name`, `durable`, `schedule`, `prompt`, `sessionTarget: isolated`, `model` exactly as declared in `setup/cron/heartbeat.md` (and matching `modelTiers.cheap`), no `to`, `payload.kind: agentTurn`, `payload.lightContext: true`, `timeout-seconds: 600`
- `reminder-check`: `name`, `durable`, `schedule`, `prompt`, `sessionTarget: isolated`, `model` exactly as declared in `setup/cron/reminder-check.md` (and matching `modelTiers.cheap`), no `to`, `payload.kind: agentTurn`, `payload.lightContext: true`, `timeout-seconds: 300`
- `pull-main`: `name`, `durable`, `schedule`, `prompt`, `sessionTarget: isolated`, `model` exactly as declared in `setup/cron/pull-main.md` (and matching `modelTiers.cheap`), no `to`, `payload.kind: agentTurn`, `payload.lightContext: true`, `timeout-seconds: 600`
- `janitor`: `name`, `durable`, `schedule`, `prompt`, `sessionTarget: isolated`, `model` exactly as declared in `setup/cron/janitor.md` (decoupled from cheap tier), no `to`, `payload.kind: agentTurn`, `payload.lightContext: false`, `timeout-seconds: 1800`

All match → report nothing. Any corrected → note which + what drift fixed.

### 4. Environment Check
- Verify `.env` exists + contains NOTION_API_KEY and NOTION_DATABASE_ID
- Verify `.env` contains OPS_ALERT_SIGNAL_NUMBER so janitor findings can be surfaced
- Verify optional reminder settings are sane: if REMINDER_SIGNAL_FILE is present, it must be a repo-root filename only, not an empty value, path, `.`, or `..`
- Do not print secret values. Report only missing/invalid keys.

### 6. Notion and State Audit
- Query for pending reminders with `Remind At` in the past that are still `Pending`. Surface counts and page IDs/titles to the operator; do not auto-complete or delete them.
- Look for recurring tasks whose next expected instance appears missing. Surface likely gaps for operator review; do not synthesize tasks without confirmation.
- Read `state.json` if present. Prune expired `recent_outbound` entries only if doing so is a straightforward state maintenance write that preserves all other fields via temp file + rename; otherwise surface the stale entries for operator decision.

### 7. Memory Rot Check
- Inspect `memory/YYYY-MM-DD.md` files older than 90 days for obvious stale operational facts that contradict newer docs or `MEMORY.md`.
- Inspect `MEMORY.md` for entries that contradict newer learned preferences or documented system behavior.
- Surface contradictions or cleanup candidates. Do not delete memory without operator confirmation.

### 8. Cron Run-History Review
- Review available OpenClaw cron run history for canonical jobs.
- Surface jobs with rising `consecutiveErrors`, repeated failures, unusually long `lastDurationMs` outliers, or runs that consistently time out.
- Include job name, observed symptom, and the smallest useful next action.

### 9. Janitor Summary
- If anything actionable was found or corrected, send one end-of-run summary to `OPS_ALERT_SIGNAL_NUMBER` via Signal. Include concise bullets grouped by area: cron drift, environment, Notion/state, memory, run history.
- If nothing needs attention, stay silent or reply only with `NO_REPLY` if the runtime requires an output token.
