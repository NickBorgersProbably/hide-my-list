# Cron Job: pull-main

Keeps workspace synced with origin/main. Script-driven for Git hygiene: `scripts/pull-main.sh` handles clean pulls + dirty-state recovery (GitHub issue creation + repo reset) without agent reasoning.

## Registration

```
CronCreate:
  schedule: "0 */2 * * *"
  durable: true
  name: "pull-main"
  sessionTarget: isolated
  model: litellm/qwen2.5  # must match setup/model-tiers.json cheap
  payload:
    kind: agentTurn
    lightContext: true  # empty bootstrap — cron prompt is self-contained
  timeout-seconds: 600
```

Isolated cheap-tier maintenance session (see `setup/model-tiers.json`). Executes `scripts/pull-main.sh`, stays silent (`NO_REPLY`). Cron spec re-application after pulls handled by weekly janitor drift correction (`docs/heartbeat-checks.md` Check 2b), not this job — isolated session can't reliably call CronList/CronUpdate.

The script is fully self-contained, so the LLM adds no value after process launch. Current OpenClaw durable cron registration uses `agentTurn`; the reduced cadence limits routine LLM use.

## Prompt

```
Run scripts/pull-main.sh.
Reply with ONLY: NO_REPLY
```

## Notes

- Script handles Git-state recovery: clean pulls silent, dirty pulls create GitHub issue (preserving local changes) then reset repo.
- Two-hour cadence limits routine GPU use while preserving workspace freshness through the daily heartbeat dirty-pull recovery safety net.
- If `gh` auth missing, falls back to `GITHUB_PAT` from `.env` via `GH_TOKEN`. If neither available, leaves `.pull-dirty` for HEARTBEAT backstop (section 5) to retry via `scripts/pull-main.sh --recover-only` after auth restored. Until then, heartbeat preserves signal + surfaces problem for operator.
- AGENTS.md startup and the `heartbeat` cron (`docs/heartbeat-checks.md` Check 2) re-register missing jobs. Weekly `janitor` (`docs/heartbeat-checks.md` Check 2b) patches drift — guards against manual deletion, gateway data loss, or other failure modes that drop or stale the canonical job.
- Post-pull cron spec re-application handled by janitor within the next weekly cycle. Delay acceptable — cron spec changes are rare, not user-facing, and missing jobs are repaired sooner by startup/heartbeat existence checks.
- GitHub issue preserves local changes for PR-based review before incorporation. Enforces design principle: structural/prompt changes go through external review.
