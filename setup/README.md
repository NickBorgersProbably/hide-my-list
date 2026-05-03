# hide-my-list — Setup & Operations

## Prerequisites

- **OpenClaw** installed and configured (`openclaw setup`)
- **Notion** database created with the schema from `docs/notion-schema.md`
- **Signal** account configured if Signal will be one of the interactive messaging surfaces
- **LiteLLM proxy** for the default model routing (`qwen2.5` cheap tier plus Anthropic-backed medium/expensive tiers)

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

   Before continuing, make sure `USER.md` has the correct timezone line for the
   user, including the IANA TZ identifier in parentheses, such as
   `America/Chicago`.

   Bootstrap also creates `~/.openclaw/media/` and
   `~/.openclaw/media/outbound/` with `0755` permissions so OpenClaw-staged
   attachments remain readable to Signal.

5. Configure OpenClaw:
   ```bash
   # Copy and customize the config template
   cp setup/openclaw.json.template ~/.openclaw/openclaw.json
   # Edit ~/.openclaw/openclaw.json — replace all {{PLACEHOLDER}} values
   ```

   `agents.defaults.envelopeTimezone` is a required first-setup field. Set it to
   the same IANA timezone identifier from `USER.md`.

   Example:
   ```json
   {
     "agents": {
       "defaults": {
         "envelopeTimezone": "America/Chicago"
       }
     }
   }
   ```

   Without `envelopeTimezone`, OpenClaw injects `Current time:` in UTC, so the
   agent can misread relative dates like "tomorrow" when the user's local date
   differs from UTC.

6. Start the gateway:
   ```bash
   openclaw gateway
   ```

7. Register cron jobs (from within an agent session or via the control UI):
   - See `setup/cron/heartbeat.md` for system health checks and cron drift repair
   - See `setup/cron/reminder-check.md` for the reminder polling job
   - See `setup/cron/pull-main.md` for automatic workspace sync

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `NOTION_API_KEY` | Yes | Notion integration API key |
| `NOTION_DATABASE_ID` | Yes | ID of the tasks database |
| `OPENAI_API_KEY` | No | For AI-generated reward images |
| `GITHUB_PAT` | No | Personal access token used by repo maintenance scripts when `gh auth login` has not been run on the host |
| `REMINDER_SIGNAL_FILE` | No | Optional reminder handoff filename in the repo root (defaults to `.reminder-signal`) |
| `OPS_ALERT_SIGNAL_NUMBER` | Yes | Separate Signal recipient for heartbeat ops alerts (Notion failures, cron drift/expiry, dirty-pull recovery problems, malformed reminder handoffs) |
| `CODEX_MODEL` | No | Overrides the Codex CLI model (defaults to `gpt-5.5` for the shared LiteLLM proxy) |

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
| reminder-`<page_id>` | One-shot at `remind_at` (registered at intake) | User-facing reminder delivery; self-deletes on success |
| reminder-check | Every 15 min | Safety-net poll for reminders the one-shot failed to deliver; writes the `.reminder-signal` handoff file for AGENTS.md step 5 / heartbeat Check 1 to consume |
| pull-main | Every 10 min | Pull `origin/main` and recover from dirty tracked-file states |
| heartbeat | Every 2 hours | System health, recurring-cron re-registration, cron drift correction, stranded reminder delivery, ops alerts to the separate operator Signal recipient |

The `heartbeat` cron (every 2 hours) re-registers any missing canonical recurring cron job (`heartbeat`, `reminder-check`, `pull-main`) and patches live drift back to the `setup/cron/` specs — guards against manual deletion, gateway data loss, or other failure modes that drop the job. One-shot `reminder-<page_id>` jobs are out of scope for this check; they self-delete after firing, and the recurring `reminder-check` poll catches any that fail to fire. Recurring jobs run as isolated cron sessions (`sessionTarget: isolated`, cheap-tier model per `modelTiers` in `setup/openclaw.json.template`, `payload.kind: agentTurn`, `payload.lightContext: true` — empty bootstrap context since the prompts are self-contained scripts/spec readers). The one-shot delivery cron uses `sessionTarget: main`, model `litellm/claude-haiku-4-5`, and `lightContext: false` so the fired agent turn has SOUL.md tone + AGENTS.md state.json conventions in scope (full contract in `setup/cron/reminder-delivery.md`). Built-in OpenClaw heartbeat is disabled in `setup/openclaw.json.template` with `agents.defaults.heartbeat.every: 0`.

Production recommendation: rely on the one-shot cron for primary delivery (fires at exact `remind_at`); keep `reminder-check` at 15-minute cadence and heartbeat every 2 hours as the safety net. In the unlikely fallback case where the one-shot fails to fire, reminder delivery can take up to about 135 minutes via the polling path before a user interaction picks it up.

## Customizing Model Tiers

Model assignments use a tier system defined in `setup/openclaw.json.template` under `modelTiers`:

| Tier | Role | Default |
|------|------|---------|
| `expensive` | Primary interactive agent | `claude-opus-4-6` |
| `medium` | Fallback | `claude-sonnet-4-6` |
| `cheap` | Isolated recurring cron jobs (`heartbeat`, `reminder-check`, `pull-main`) | `qwen2.5` |
| Decoupled direct model | One-shot reminder delivery; multi-step user-facing state mutation | `claude-haiku-4-5` |

Default setup assumes LiteLLM fronts every configured model. If you want a direct Anthropic-only install, that is a custom setup: remap `modelTiers.cheap` to an Anthropic model you can access, then update the cheap-tier cron `model:` lines before first run. One-shot reminder delivery is configured directly and only needs to reference a model ID present in the template.

To remap tiers to your available models:

1. Add your models to the `models[]` array in `setup/openclaw.json.template`
2. Edit `modelTiers` values to point at your model IDs
3. Update `agents.defaults` in the same file to match: `model.primary` = `litellm/<expensive>`, `model.fallbacks` = `[litellm/<medium>]`, and keep the built-in `heartbeat.every` disabled (`0`)
4. Update `model:` lines in `setup/cron/heartbeat.md`, `setup/cron/reminder-check.md`, and `setup/cron/pull-main.md` to `litellm/<cheap>`
5. Run `bash scripts/validate-model-refs.sh` — catches drift between tiers, agent config, cron specs, and documented defaults

Most narrative docs use tier names ("cheap-tier", "medium-tier"). Concrete config snippets in `setup/cron/` and `docs/openclaw-integration.md` describe current defaults and must stay aligned when you remap tiers or direct-model assignments.

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
- Keep `heartbeat.target` unset or `"none"` if present in a live config; the supported delivery path is the explicit `message` call from `docs/heartbeat-checks.md` Check 1, not generic heartbeat reply routing
- Confirm the Signal channel itself is configured and healthy in `openclaw.json`

**Signal attachments fail with `Permission denied`:**
- Re-run `bash setup/bootstrap.sh` to recreate or repair the OpenClaw media staging directories with the expected permissions.
- If you need the repair immediately, run `chmod 755 ~/.openclaw/media ~/.openclaw/media/outbound`.
- Verify both directories are traversable by the Signal process: `namei -om ~/.openclaw/media/outbound`.

**Agent not responding:**
- Check `openclaw status` for channel health
- Check gateway logs: `openclaw logs`

**Relative dates land on the wrong day:**
- Confirm `USER.md` has the correct IANA timezone identifier in the `Timezone`
  line
- Confirm `~/.openclaw/openclaw.json` sets `agents.defaults.envelopeTimezone` to
  that same identifier
- Restart the gateway after changing `openclaw.json`

**Cron jobs disappeared:**
- If a canonical recurring cron job (`reminder-check`, `pull-main`) goes missing, the next `heartbeat` cron run (every 2 hours) will re-register it from `setup/cron/`.
- If the `heartbeat` cron itself disappeared, manually re-register it from `setup/cron/heartbeat.md`; once running, it can patch its own drift.
- One-shot `reminder-<page_id>` jobs are expected to disappear after firing (`deleteAfterRun: true`). If a one-shot fails to fire before its scheduled time and goes missing, the recurring `reminder-check` poll will pick the still-Pending Notion row up and deliver via the safety net.
- Or manually re-register per the definitions in `setup/cron/`

**Built-in heartbeat still runs after pulling the latest template:**
- `scripts/pull-main.sh` writes `.config-drift` when `setup/openclaw.json.template` changes. On the next main-agent startup/interaction, `AGENTS.md` step 6 parses the template's `agents.defaults.heartbeat` subtree, compares it with `openclaw config get 'agents.defaults.heartbeat'`, and realigns drift by setting the whole subtree with `openclaw config set 'agents.defaults.heartbeat' '<template-heartbeat-json>' --strict-json`. Whole-subtree repair also drops stale live keys removed from the template.
- Verify the repair ran: `.config-drift` should be gone after the next main-agent session, and the live `agents.defaults.heartbeat` block should match the template, especially `every: 0`.
- If you need the change before another main-agent session runs, or `.config-drift` remains because config repair failed, compare `setup/openclaw.json.template` against the live config and realign the whole subtree with `openclaw config set 'agents.defaults.heartbeat' '<template-heartbeat-json>' --strict-json`.
- Do not preserve a stale enabled built-in heartbeat. The template's `agents.defaults.heartbeat` subtree is canonical for this repair flow.
- Then restart the gateway: `openclaw gateway`.
- Verify: `openclaw cron list` shows the durable `heartbeat` job from `setup/cron/heartbeat.md`, and no built-in heartbeat sessions continue to run.

**Git pull conflicts:**
- Agent-edited files (MEMORY.md, memory/*.md) are gitignored so won't conflict
- If HEARTBEAT.md or AGENTS.md conflict, the repo version is authoritative
