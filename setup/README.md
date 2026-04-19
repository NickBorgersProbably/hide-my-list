# hide-my-list — Setup & Operations

## Prerequisites

- **OpenClaw** installed and configured (`openclaw setup`)
- **Notion** database created with the schema from `docs/notion-schema.md`
- **Signal** account configured if Signal will be one of the interactive messaging surfaces
- **LiteLLM proxy** for the default model routing (`gemma4-small` cheap tier plus Anthropic-backed medium/expensive tiers)

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

2. Install the repo-managed git hooks for this worktree:
   ```bash
   cd ~/.openclaw/workspace
   bash .githooks/install-hooks.sh
   ```

   `pre-commit` handles the fast staged-file checks, and `pre-push` reruns the
   deterministic CI-equivalent checks for changed scripts, docs, and
   workflow-related paths before GitHub is the first place they fail.

3. Create the `.env` file:
   ```bash
   cp ~/.openclaw/workspace/.env.template ~/.openclaw/workspace/.env
   # Then edit ~/.openclaw/workspace/.env with the values you need
   ```

   Runtime scripts load only the specific variables they request from `.env`, so
   one file remains the source of truth without handing every credential to every
   script. For advanced/manual workflows, set `HIDE_MY_LIST_ENV_FILE` to point
   helper scripts that source `scripts/load-env.sh` at a different env file;
   that includes the Notion, reminder, and GitHub-auth helpers. Normal runtime
   setups should leave it unset and keep using `~/.openclaw/workspace/.env`.

4. Run bootstrap:
   ```bash
   cd ~/.openclaw/workspace
   bash setup/bootstrap.sh
   ```

5. Configure OpenClaw:
   ```bash
   # Copy and customize the config template
   cp setup/openclaw.json.template ~/.openclaw/openclaw.json
   # Edit ~/.openclaw/openclaw.json — replace all {{PLACEHOLDER}} values
   ```

6. Start the gateway:
   ```bash
   openclaw gateway
   ```

7. Register cron jobs (from within an agent session or via the control UI):
   - See `setup/cron/reminder-check.md` for the reminder polling job
   - See `setup/cron/pull-main.md` for automatic workspace sync
   - The heartbeat is built-in and configured in `openclaw.json`

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `NOTION_API_KEY` | Yes | Notion integration API key |
| `NOTION_DATABASE_ID` | Yes | ID of the tasks database |
| `OPENAI_API_KEY` | No | For AI-generated reward images |
| `GITHUB_PAT` | No | Personal access token used by repo maintenance scripts when `gh auth login` has not been run on the host |
| `REMINDER_SIGNAL_FILE` | No | Optional reminder handoff filename in the repo root (defaults to `.reminder-signal`) |
| `OPS_ALERT_SIGNAL_NUMBER` | Yes | Separate Signal recipient for heartbeat ops alerts (Notion failures, cron drift/expiry, dirty-pull recovery problems, malformed reminder handoffs) |
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
| reminder-check | Every 15 min | Poll Notion for due reminders, write the reminder handoff file (delivery via heartbeat/startup check) |
| pull-main | Every 10 min | Pull `origin/main` and recover from dirty tracked-file states |
| heartbeat (built-in) | Every 60 min | System health, cron re-registration, cron drift correction, stranded reminder delivery, ops alerts to the separate operator Signal recipient |

Cron jobs auto-expire after 7 days. The heartbeat re-registers missing jobs automatically and patches live cron jobs back to the `setup/cron/` specs if they drift. Both jobs run as isolated cron sessions (`sessionTarget: isolated`, cheap-tier model per `modelTiers` in `setup/openclaw.json.template`, `payload.kind: agentTurn`).

Production recommendation: keep `reminder-check` at 15-minute cadence and heartbeat hourly as the default production cost/latency tradeoff for routine or low-stakes reminders. In the fully idle case, reminder delivery can take up to about 75 minutes under this deferred-delivery design, so exact-time reminders are not guaranteed unless the architecture changes.

## Customizing Model Tiers

Model assignments use a tier system defined in `setup/openclaw.json.template` under `modelTiers`:

| Tier | Role | Default |
|------|------|---------|
| `expensive` | Primary interactive agent | `claude-opus-4-6` |
| `medium` | Heartbeat, fallback | `claude-sonnet-4-6` |
| `cheap` | Isolated cron jobs | `gemma4-small` |

Default setup assumes LiteLLM fronts every configured model. If you want a direct Anthropic-only install, that is a custom setup: remap `modelTiers.cheap` to an Anthropic model you can access, then update the cron `model:` lines and agent defaults to match before first run.

To remap tiers to your available models:

1. Add your models to the `models[]` array in `setup/openclaw.json.template`
2. Edit `modelTiers` values to point at your model IDs
3. Update `agents.defaults` in the same file to match: `model.primary` = `litellm/<expensive>`, `model.fallbacks` = `[litellm/<medium>]`, `heartbeat.model` = `litellm/<medium>`
4. Update `model:` lines in `setup/cron/reminder-check.md` and `setup/cron/pull-main.md` to `litellm/<cheap>`
5. Run `bash scripts/validate-model-refs.sh` — catches drift between tiers, agent config, cron specs, and documented defaults

Most narrative docs use tier names ("cheap-tier", "medium-tier"). Concrete config snippets in `setup/cron/` and `docs/openclaw-integration.md` describe current defaults and must stay aligned when you remap tiers.

## Updating

```bash
cd ~/.openclaw/workspace
git pull origin main
```

The agent reads docs on every interaction, so changes take effect immediately. No restart needed unless `openclaw.json` changed.

## Contributor Hooks

Install hooks in every worktree before committing:

```bash
bash .githooks/install-hooks.sh
```

The hook contract is:
- `pre-commit` runs fast staged-file checks.
- `pre-push` reruns the deterministic CI-equivalent checks for changed scripts, docs, and workflow-related paths, so those failures are caught locally before GitHub is the first place they fail.

`core.hooksPath` is stored per worktree, so `git worktree add` requires re-running `bash .githooks/install-hooks.sh` inside the new worktree.

Manual regression playbook:
1. Run `bash .githooks/install-hooks.sh`.
2. Create a feature branch.
3. Introduce a deliberate workflow lint error, such as an invalid GitHub Actions expression in `.github/workflows/pr-tests.yml`.
4. Commit the change and run `git push origin HEAD`.
5. Confirm that `pre-push` rejects the push locally with the same deterministic diagnostic CI would have produced.

## Troubleshooting

**Reminders not firing:**
- Reminder delivery uses the OpenClaw `message` tool with `channel: signal` — it does not rely on `heartbeat.target` or session reply routing. Verify that the Signal channel is configured and enabled in `openclaw.json`.
- `heartbeat.target` should stay unset or `"none"`. Heartbeat operator alerts use explicit `message(..., channel: signal, target: OPS_ALERT_SIGNAL_NUMBER)` routing instead of generic heartbeat output.
- Check that the reminder-check cron is registered (ask the agent to check CronList)
- Verify `.env` has correct `NOTION_API_KEY` and `NOTION_DATABASE_ID`
- Run `scripts/check-reminders.sh` manually to test Notion connectivity
- If you overrode `REMINDER_SIGNAL_FILE`, verify it is just a filename and that the repo root is writable

**Heartbeat ops alerts not arriving:**
- Verify `.env` contains `OPS_ALERT_SIGNAL_NUMBER` and that it points to the intended operator Signal recipient
- Keep `heartbeat.target` unset or `"none"`; the supported delivery path is the explicit `message` call from `docs/heartbeat-checks.md` Check 1, not generic heartbeat reply routing
- Confirm the Signal channel itself is configured and healthy in `openclaw.json`

**Agent not responding:**
- Check `openclaw status` for channel health
- Check gateway logs: `openclaw logs`

**Cron jobs disappeared:**
- They auto-expire after 7 days. The next heartbeat (every 60 min) will re-register them.
- Or manually re-register per the definitions in `setup/cron/`

**Git pull conflicts:**
- Agent-edited files (MEMORY.md, memory/*.md) are gitignored so won't conflict
- If HEARTBEAT.md or AGENTS.md conflict, the repo version is authoritative
