# HEARTBEAT.md

## Ops Alert Routing

- Keep `heartbeat.target` unset (or `"none"`) and do not rely on generic heartbeat reply routing for operational alerts.
- When any check needs operator attention, resolve the dedicated Signal recipient from `.env` with the same helper pattern the scripts use:
  ```bash
  OPS_ALERT_SIGNAL_NUMBER="$(bash -lc 'SCRIPT_DIR=$(cd "$(dirname scripts/check-reminders.sh)" && pwd); source "$SCRIPT_DIR/load-env.sh" OPS_ALERT_SIGNAL_NUMBER?; test -n "$OPS_ALERT_SIGNAL_NUMBER"; printf "%s\n" "$OPS_ALERT_SIGNAL_NUMBER"')"
  ```
- Send every ops alert explicitly with the OpenClaw `message` tool using `action: send`, `channel: signal`, and `target: OPS_ALERT_SIGNAL_NUMBER`.
- Use this explicit route for operator-facing failures or corrections such as malformed reminder handoffs, cron expiry/drift repair, Notion connectivity failures, missing required env vars, and dirty-pull recovery problems.
- Treat `OPS_ALERT_SIGNAL_NUMBER` as a hard required env prerequisite. Do not rely on heartbeat reply routing as a fallback ops-alert path.

## Checks (in order)

### 1. Stranded Reminder Signal
- Resolve the reminder handoff file with the same helper and repo-root filename validation the scripts use, so `.env` overrides are honored:
  ```bash
  HANDOFF_FILE="$(bash -lc 'SCRIPT_DIR=$(cd "$(dirname scripts/check-reminders.sh)" && pwd); ROOT_DIR=$(cd "$SCRIPT_DIR/.." && pwd); source "$SCRIPT_DIR/load-env.sh" REMINDER_SIGNAL_FILE?; SIGNAL_BASENAME=${REMINDER_SIGNAL_FILE:-.reminder-signal}; case "$SIGNAL_BASENAME" in ""|"."|".."|*/*) echo "invalid REMINDER_SIGNAL_FILE" >&2; exit 1 ;; esac; printf "%s\n" "$ROOT_DIR/$SIGNAL_BASENAME"')"
  ```
- This file is the reminder handoff written by `scripts/check-reminders.sh`
- If `HANDOFF_FILE` still exists when heartbeat runs, treat it as undelivered reminder work: read and validate it (must be JSON with a `reminders` array where each entry is an object with string `page_id`, non-empty string `title`, and `status` exactly `sent` or `missed`; any other shape or status makes the handoff malformed. If malformed, leave the file in place, send an explicit ops alert to `OPS_ALERT_SIGNAL_NUMBER`, and do not deliver any entries, call `complete-reminder`, or delete the handoff file). For each valid reminder, send it to Signal using the OpenClaw `message` tool (`action: send`, `channel: signal`), then run `scripts/notion-cli.sh complete-reminder PAGE_ID sent|missed` based on the file, then delete that handoff file. If reminder delivery or `complete-reminder` fails, leave the handoff file in place and send an explicit ops alert to `OPS_ALERT_SIGNAL_NUMBER`.
- This is the hourly reminder-delivery backstop in the current design. The isolated `reminder-check` cron only writes the handoff file — it does not deliver. Delivery happens here (every 60 min) and opportunistically via the AGENTS.md startup check (on every user interaction).

### 2. Cron Job Health
Verify that durable cron jobs are registered. If any are missing, re-register them.

| Job | Schedule | Action |
|-----|----------|--------|
| reminder-check | `*/15 * * * *` | Run `scripts/check-reminders.sh` (query-only; writes the reminder handoff file if reminders due) |
| pull-main | `*/10 * * * *` | Run `scripts/pull-main.sh`; the script handles dirty-pull recovery |

To check: use CronList. If a job is missing (7-day auto-expiry), re-create it with CronCreate (durable: true) using the schedule, prompt, and options from `setup/cron/`. Both jobs run as isolated Haiku sessions with `sessionTarget: isolated`, `model: litellm/claude-haiku-4-5`, `payload.kind: agentTurn`, and `timeout-seconds: 60`. Cron jobs should never deliver directly to Signal or any other channel on their own. If any job had to be re-created, send an explicit ops alert to `OPS_ALERT_SIGNAL_NUMBER` naming the missing job(s).

### 2b. Cron Spec Drift Check
For each registered cron job (`reminder-check`, `pull-main`), compare the live job's effective registration against the canonical `CronCreate` spec in `setup/cron/<name>.md`.

To check: use CronList to inspect the live registrations, then read the corresponding spec file in `setup/cron/`.

At minimum, compare and correct these fields:
- `name`
- `durable`
- `schedule`
- `prompt`
- `sessionTarget` (canonical: `isolated` for both jobs)
- `model` (canonical: `litellm/claude-haiku-4-5` for both jobs)
- direct-delivery routing field: live `to` if present (should not exist)
- payload field: canonical `payload.kind`
- `timeout-seconds`

If a stale `pipeline-monitor` cron is still registered, delete it with CronDelete — that job has been removed.

If any field differs from the spec, patch the live job to match with CronUpdate. If CronUpdate cannot safely change an identity field such as `name` or `durable`, delete and re-create the job from the spec instead of leaving drift in place. Preserve the intended durable registration contract from the spec:
- `reminder-check`: `name`, `durable`, `schedule`, `prompt`, `sessionTarget: isolated`, `model: litellm/claude-haiku-4-5`, no `to`, `payload.kind: agentTurn`, `timeout-seconds: 60`
- `pull-main`: `name`, `durable`, `schedule`, `prompt`, `sessionTarget: isolated`, `model: litellm/claude-haiku-4-5`, no `to`, `payload.kind: agentTurn`, `timeout-seconds: 60`

If all jobs already match their specs, do not report anything. If any jobs were corrected, briefly note which ones were patched and what drift was fixed, and send that summary as an explicit ops alert to `OPS_ALERT_SIGNAL_NUMBER`. If drift could not be corrected safely, send an explicit ops alert describing the failure.

### 3. Notion Connectivity
- Run `scripts/notion-cli.sh query-pending` with a short timeout
- If it fails, send an explicit ops alert to `OPS_ALERT_SIGNAL_NUMBER` with the error — don't retry aggressively

### 4. Environment Check
- Verify `.env` exists and contains `NOTION_API_KEY`, `NOTION_DATABASE_ID`, and `OPS_ALERT_SIGNAL_NUMBER`
- If `NOTION_API_KEY` or `NOTION_DATABASE_ID` is missing, send an explicit ops alert to `OPS_ALERT_SIGNAL_NUMBER`
- If `OPS_ALERT_SIGNAL_NUMBER` is missing, fail the environment check and keep the heartbeat result concise; ops alerts are unavailable until the env is fixed

### 5. Dirty Pull Recovery (safety net)
- If `.pull-dirty` exists and is older than 20 minutes, the pull-main cron may have failed to recover
- Run `scripts/pull-main.sh --recover-only` to retry after the underlying problem is fixed (for example restoring interactive `gh` authentication, exporting a valid `GH_TOKEN`, or providing token-based auth through repo `.env` `GITHUB_PAT`, which the helper exports as `GH_TOKEN`). The script creates the GitHub issue and resets the repo when recovery can proceed.
- If recovery still does not complete, send an explicit ops alert to `OPS_ALERT_SIGNAL_NUMBER`
- Normally the pull-main cron handles recovery automatically. This check is a backstop for cases where GitHub auth was unavailable or the script errored; until either interactive `gh` auth, a valid exported `GH_TOKEN`, or repo `.env` `GITHUB_PAT` is available again, heartbeat will preserve `.pull-dirty` and send the problem to `OPS_ALERT_SIGNAL_NUMBER` rather than clearing it

That's it. If nothing needs attention, reply `HEARTBEAT_OK`. If you had to send any explicit ops alerts, keep the heartbeat reply concise and informational, but do not rely on that reply text for alert delivery.
