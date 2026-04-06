# hide-my-list — Setup & Operations

## Prerequisites

- **OpenClaw** installed and configured (`openclaw setup`)
- **Notion** database created with the schema from `docs/notion-schema.md`
- **Signal** account configured for scheduled cron delivery (`SIGNAL_OWNER_NUMBER` must point at the operator/user to notify)
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
   cat > ~/.openclaw/workspace/.env << 'EOF'
   NOTION_API_KEY=ntn_your_key_here
   NOTION_DATABASE_ID=your_database_id_here
   SIGNAL_OWNER_NUMBER=+15551234567   # Required: Signal recipient for durable cron output
   OPENAI_API_KEY=sk-your_key_here    # Optional: for reward image generation
   GITHUB_PAT=ghp_your_token_here     # Optional: for higher GitHub API rate limits
   EOF
   ```

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
   - See `setup/cron/pipeline-monitor.md` for GitHub monitoring
   - See `setup/cron/pull-main.md` for automatic workspace sync
   - The heartbeat is built-in and configured in `openclaw.json`

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `NOTION_API_KEY` | Yes | Notion integration API key |
| `NOTION_DATABASE_ID` | Yes | ID of the tasks database |
| `SIGNAL_OWNER_NUMBER` | Yes | Signal recipient (E.164 format) used by durable cron jobs |
| `OPENAI_API_KEY` | No | For AI-generated reward images |
| `GITHUB_PAT` | No | GitHub personal access token for higher rate limits |
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
| reminder-check | Every 5 min | Poll Notion for due reminders, write `.reminder-signal`, deliver to user |
| pipeline-monitor | Every 2 min | Check GitHub for PR/CI status changes |
| pull-main | Every 10 min | Pull `origin/main` and recover from dirty tracked-file states |
| heartbeat (built-in) | Every 30 min | System health, cron re-registration, cron drift correction |

Cron jobs auto-expire after 7 days. The heartbeat re-registers missing jobs automatically and patches live cron jobs back to the `setup/cron/` specs if they drift.

## Updating

```bash
cd ~/.openclaw/workspace
git pull origin main
```

The agent reads docs on every interaction, so changes take effect immediately. No restart needed unless `openclaw.json` changed.

## Troubleshooting

**Reminders not firing:**
- Check that the reminder-check cron is registered (ask the agent to check CronList)
- Verify `.env` has correct `NOTION_API_KEY`, `NOTION_DATABASE_ID`, and `SIGNAL_OWNER_NUMBER`
- Run `scripts/check-reminders.sh` manually to test Notion connectivity

**Agent not responding:**
- Check `openclaw status` for channel health
- Check gateway logs: `openclaw logs`

**Cron jobs disappeared:**
- They auto-expire after 7 days. The next heartbeat (every 30 min) will re-register them.
- Or manually re-register per the definitions in `setup/cron/`

**Git pull conflicts:**
- Agent-edited files (MEMORY.md, memory/*.md) are gitignored so won't conflict
- If HEARTBEAT.md or AGENTS.md conflict, the repo version is authoritative
