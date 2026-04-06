# hide-my-list — Setup & Operations

## Prerequisites

- **OpenClaw** installed and configured (`openclaw setup`)
- **Notion** database created with the schema from `docs/notion-schema.md`
- **Signal** account configured if Signal will be one of the interactive messaging surfaces
- **LiteLLM proxy** (or direct Anthropic API access) for model routing

## Quick Start

1. Clone this repo as the OpenClaw workspace:
   ```bash
   # If starting fresh:
   git clone https://github.com/NickBorgersProbably/hide-my-list.git ~/.openclaw/workspace

   # If replacing an existing workspace:
   cd ~/.openclaw/workspace
   git init
   git remote add origin https://github.com/NickBorgersProbably/hide-my-list.git
   git fetch origin main
   git checkout main
   ```

2. Create the `.env` file:
   ```bash
   cp ~/.openclaw/workspace/.env.template ~/.openclaw/workspace/.env
   # Then edit ~/.openclaw/workspace/.env with the values you need
   ```

   Runtime scripts load only the specific variables they request from `.env`, so
   one file remains the source of truth without handing every credential to every
   script. For advanced/manual workflows, set `HIDE_MY_LIST_ENV_FILE` to point
   helper scripts at a different env file; normal runtime setups should leave it
   unset and keep using `~/.openclaw/workspace/.env`.

3. Run bootstrap:
   ```bash
   cd ~/.openclaw/workspace
   bash setup/bootstrap.sh
   ```

4. Configure OpenClaw:
   ```bash
   # Copy and customize the config template
   cp setup/openclaw.json.template ~/.openclaw/openclaw.json
   # Edit ~/.openclaw/openclaw.json — replace all {{PLACEHOLDER}} values
   ```

5. Start the gateway:
   ```bash
   openclaw gateway
   ```

6. Register cron jobs (from within an agent session or via the control UI):
   - See `setup/cron/reminder-check.md` for the reminder polling job
   - See `setup/cron/pull-main.md` for automatic workspace sync
   - The heartbeat is built-in and configured in `openclaw.json`

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `NOTION_API_KEY` | Yes | Notion integration API key |
| `NOTION_DATABASE_ID` | Yes | ID of the tasks database |
| `OPENAI_API_KEY` | No | For AI-generated reward images |
| `REMINDER_SIGNAL_FILE` | No | Optional override for the reminder handoff path (defaults to `.reminder-signal`) |
| `CODEX_MODEL` | No | Overrides the Codex CLI model (defaults to `gpt-5.4` for the shared LiteLLM proxy) |

Advanced overrides for self-hosted LiteLLM setups are also supported:

- `CODEX_MODEL_PROVIDER` (defaults to `litellm`)
- `CODEX_MODEL_PROVIDER_NAME` (defaults to `LiteLLM`)
- `CODEX_MODEL_BASE_URL` (defaults to `https://llm.featherback-mermaid.ts.net/v1`)
- `CODEX_MODEL_ENV_KEY` (defaults to `OPENAI_API_KEY`)

Set these before running the devcontainer bootstrap if your environment differs from the default proxy.

## Cron Jobs

The agent uses OpenClaw's durable cron system instead of bash daemons:

| Job | Schedule | Purpose |
|-----|----------|---------|
| reminder-check | Every 15 min | Poll Notion for due reminders, write the reminder handoff file (defaults to `.reminder-signal`), deliver to user |
| pull-main | Every 10 min | Pull `origin/main` and recover from dirty tracked-file states |
| heartbeat (built-in) | Every 60 min | System health, cron re-registration, cron drift correction |

Cron jobs auto-expire after 7 days. The heartbeat re-registers missing jobs automatically and patches live cron jobs back to the `setup/cron/` specs if they drift. Both jobs inject `systemEvent` payloads into the main agent session with `delivery: { mode: none }`.

Production recommendation: keep heartbeat hourly because it is only an infrastructure backstop; keep `reminder-check` at 15-minute cadence as the default cost/latency tradeoff for routine or low-stakes reminders. For exact-time reminders such as medication, departures, or meetings, use tighter polling instead of treating the 15-minute window as exact.

## Updating

```bash
cd ~/.openclaw/workspace
git pull origin main
```

The agent reads docs on every interaction, so changes take effect immediately. No restart needed unless `openclaw.json` changed.

## Troubleshooting

**Reminders not firing:**
- Check that the reminder-check cron is registered (ask the agent to check CronList)
- Verify `.env` has correct `NOTION_API_KEY` and `NOTION_DATABASE_ID`
- Run `scripts/check-reminders.sh` manually to test Notion connectivity
- If you overrode `REMINDER_SIGNAL_FILE`, verify the configured handoff path is writable

**Agent not responding:**
- Check `openclaw status` for channel health
- Check gateway logs: `openclaw logs`

**Cron jobs disappeared:**
- They auto-expire after 7 days. The next heartbeat (every 60 min) will re-register them.
- Or manually re-register per the definitions in `setup/cron/`

**Git pull conflicts:**
- Agent-edited files (MEMORY.md, memory/*.md) are gitignored so won't conflict
- If HEARTBEAT.md or AGENTS.md conflict, the repo version is authoritative
