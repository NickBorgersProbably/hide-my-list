# HEARTBEAT.md

## Checks (in order)

### 1. Cron Job Health
Verify that durable cron jobs are registered. If any are missing, re-register them.

| Job | Schedule | Action |
|-----|----------|--------|
| reminder-check | `*/15 * * * *` | Run `scripts/check-reminders.sh`, announce due reminders, then mark delivered reminders `sent`/`missed` in Notion |
| pull-main | `*/10 * * * *` | Run `scripts/pull-main.sh`; the script handles dirty-pull recovery |

To check: use CronList. If a job is missing (7-day auto-expiry), re-create it with CronCreate (durable: true) using the schedule, prompt, and options from `setup/cron/`. Both jobs run as isolated Haiku sessions with `sessionTarget: isolated`, `model: litellm/claude-haiku-4-5`, and `payload.kind: agentTurn`. `pull-main` uses `timeout-seconds: 60` and does not deliver directly. `reminder-check` uses `timeout-seconds: 120` and must keep `delivery.mode: announce` so the isolated cron turn is the single reminder-delivery path.

### 1b. Cron Spec Drift Check
For each registered cron job (`reminder-check`, `pull-main`), compare the live job's effective registration against the canonical `CronCreate` spec in `setup/cron/<name>.md`.

To check: use CronList to inspect the live registrations, then read the corresponding spec file in `setup/cron/`.

At minimum, compare and correct these fields:
- `name`
- `durable`
- `schedule`
- `prompt`
- `sessionTarget` (canonical: `isolated` for both jobs)
- `model` (canonical: `litellm/claude-haiku-4-5` for both jobs)
- delivery configuration: `reminder-check` must have `delivery.mode: announce`; `pull-main` should not have a delivery block or direct-delivery `to`
- payload field: canonical `payload.kind`
- `timeout-seconds`

If a stale `pipeline-monitor` cron is still registered, delete it with CronDelete — that job has been removed.

If any field differs from the spec, patch the live job to match with CronUpdate. If CronUpdate cannot safely change an identity field such as `name` or `durable`, delete and re-create the job from the spec instead of leaving drift in place. Preserve the intended durable registration contract from the spec:
- `reminder-check`: `name`, `durable`, `schedule`, `prompt`, `sessionTarget: isolated`, `model: litellm/claude-haiku-4-5`, `delivery.mode: announce`, `payload.kind: agentTurn`, `timeout-seconds: 120`
- `pull-main`: `name`, `durable`, `schedule`, `prompt`, `sessionTarget: isolated`, `model: litellm/claude-haiku-4-5`, no `to`, `payload.kind: agentTurn`, `timeout-seconds: 60`

If all jobs already match their specs, do not report anything. If any jobs were corrected, briefly note which ones were patched and what drift was fixed.

### 2. Notion Connectivity
- Run `scripts/notion-cli.sh query-pending` with a short timeout
- If it fails, note the error — don't retry aggressively

### 3. Environment Check
- Verify `.env` exists and contains NOTION_API_KEY and NOTION_DATABASE_ID

### 4. Dirty Pull Recovery (safety net)
- If `.pull-dirty` exists and is older than 20 minutes, the pull-main cron may have failed to recover
- Run `scripts/pull-main.sh --recover-only` to retry after the underlying problem is fixed (for example restoring interactive `gh` authentication, exporting a valid `GH_TOKEN`, or providing token-based auth through repo `.env` `GITHUB_PAT`, which the helper exports as `GH_TOKEN`). The script creates the GitHub issue and resets the repo when recovery can proceed.
- If recovery still does not complete, note the failure for operator attention
- Normally the pull-main cron handles recovery automatically. This check is a backstop for cases where GitHub auth was unavailable or the script errored; until either interactive `gh` auth, a valid exported `GH_TOKEN`, or repo `.env` `GITHUB_PAT` is available again, heartbeat will preserve `.pull-dirty` and surface the problem rather than clearing it

That's it. If nothing needs attention, reply HEARTBEAT_OK.
