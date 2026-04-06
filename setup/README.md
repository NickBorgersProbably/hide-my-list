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
   cat > ~/.openclaw/workspace/.env << 'EOF'
   NOTION_API_KEY=ntn_your_key_here
   NOTION_DATABASE_ID=your_database_id_here
   OPENAI_API_KEY=sk-your_key_here    # Optional: for reward image generation
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

   If you already have an existing `~/.openclaw/openclaw.json`, run the
   additive migration instead of overwriting it:
   ```bash
   bash setup/migrate-openclaw-config.sh
   # Restart the OpenClaw gateway after the migration so the new model is loaded
   ```

5. Start the gateway:
   ```bash
   openclaw gateway
   ```

6. Register cron jobs (from within an agent session or via the control UI):
   - See `setup/cron/reminder-check.md` for the reminder polling job
   - See `setup/cron/reminder-delivery.md` for the isolated reminder delivery job
   - See `setup/cron/pull-main.md` for automatic workspace sync
   - The heartbeat is built-in and configured in `openclaw.json`
   - Reminders will not be delivered unless both reminder cron jobs are registered

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `NOTION_API_KEY` | Yes | Notion integration API key |
| `NOTION_DATABASE_ID` | Yes | ID of the tasks database |
| `OPENAI_API_KEY` | No | For AI-generated reward images |
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
| reminder-check | Every 15 min | Poll Notion for due reminders, write `.reminder-signal` (procedural, always `NO_REPLY`) |
| reminder-delivery | Every 15 min (offset 2 min) | If `.reminder-signal` exists, deliver reminders and update Notion (isolated, Haiku-pinned) |
| pull-main | Every 10 min | Pull `origin/main` and recover from dirty tracked-file states |
| heartbeat (built-in) | Every 60 min | System health, cron re-registration, cron drift correction |

Cron jobs auto-expire after 7 days. The heartbeat re-registers missing jobs automatically and patches live cron jobs back to the `setup/cron/` specs if they drift. `reminder-check` and `pull-main` inject `systemEvent` payloads into the main agent session with `delivery: { mode: none }`. `reminder-delivery` runs in an isolated session (no `sessionTarget: main`) with `model: litellm/claude-haiku-4-5` and `delivery: { mode: best-effort-deliver }` — this isolation keeps the reminder flow at [BC] in the trust model by avoiding untrusted GitHub content from the main session.

Production recommendation: keep heartbeat hourly because it is only an infrastructure backstop; keep `reminder-check` at 15-minute cadence as the default cost/latency tradeoff for routine or low-stakes reminders. For exact-time reminders such as medication, departures, or meetings, use tighter polling instead of treating the 15-minute window as exact.

## Updating

```bash
cd ~/.openclaw/workspace
git pull origin main
```

If the pulled change introduces a new pinned model (for example the Haiku-backed
`reminder-delivery` cron), migrate the existing OpenClaw config and restart the
gateway before expecting heartbeat or `pull-main` to register that cron:

```bash
bash setup/migrate-openclaw-config.sh
```

The agent reads docs on every interaction, so changes take effect immediately. Restart only if `openclaw.json` changed.

## Troubleshooting

**Reminders not firing:**
- Check that both `reminder-check` and `reminder-delivery` crons are registered (ask the agent to check CronList)
- Verify `.env` has correct `NOTION_API_KEY` and `NOTION_DATABASE_ID`
- Run `scripts/check-reminders.sh` manually to test Notion connectivity
- Check if `.reminder-signal` exists — if it does, delivery may be failing

**Agent not responding:**
- Check `openclaw status` for channel health
- Check gateway logs: `openclaw logs`

**Cron jobs disappeared:**
- They auto-expire after 7 days. The next heartbeat (every 60 min) will re-register them.
- Or manually re-register per the definitions in `setup/cron/`

**Git pull conflicts:**
- Agent-edited files (MEMORY.md, memory/*.md) are gitignored so won't conflict
- If HEARTBEAT.md or AGENTS.md conflict, the repo version is authoritative
