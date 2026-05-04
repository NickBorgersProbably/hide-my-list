# Cron Job: janitor

Runs weekly via OpenClaw's durable cron system as a deep operational audit.

## Registration

```
CronCreate:
  schedule: "0 7 * * 1"  # Mondays at 02:00 CT — quiet hour
  durable: true
  name: "janitor"
  sessionTarget: isolated
  model: litellm/claude-opus-4-6  # decoupled from modelTiers — opus for reasoning depth
  payload:
    kind: agentTurn
    lightContext: false  # full bootstrap — janitor needs SOUL.md + AGENTS.md context
  timeout-seconds: 1800
```

Isolated high-reasoning maintenance session. Unlike the daily heartbeat, janitor loads full bootstrap context and uses Opus directly so it can audit slow-moving inconsistencies without pushing that cost into routine health sweeps.

Janitor is part of the canonical recurring cron catalog. Daily heartbeat and the main-agent startup flow ensure the `janitor` job exists. Janitor owns full recurring cron drift comparison and deeper state/data audits.

## Prompt

```
You are the weekly janitor cron for hide-my-list.

Read docs/heartbeat-checks.md and execute the Weekly Janitor Checks. Use that file as the authoritative contract for cron drift correction, environment and secrets sanity checks, Notion/state/memory audits, cron run-history review, ops alerts, and final output.
```

## Notes

- `janitor` is intentionally not tied to the cheap tier in `setup/model-tiers.json`. The model is concrete and must exist in `setup/openclaw.json.template`, but it is decoupled from tier remapping because this job is a weekly reasoning-heavy audit.
- Janitor surfaces actionable findings to the operator via Signal using `OPS_ALERT_SIGNAL_NUMBER`. It stays silent on a clean week.
- Janitor may patch cron registration drift, but it should not auto-prune suspicious Notion data; findings that require judgment are surfaced for operator decision.
