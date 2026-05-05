# Agent Capabilities and Operational Boundaries

Defines runtime roles in hide-my-list's OpenClaw deployment. Answers which session does which work.

Use as source of truth when updating `AGENTS.md`, `HEARTBEAT.md`, `docs/heartbeat-checks.md`, `setup/cron/`, or any operational doc assuming tool contract.

## Why This Exists

hide-my-list runs multiple OpenClaw session types:

- **main agent** — user talks to
- isolated **durable cron sessions** like `heartbeat`, `reminder-check`, `reminder-delivery-sweep`, `pull-main`, and `janitor`

Sessions have different responsibilities and different tools. Config patching belongs to main agent unless another session's access explicitly confirmed.

## Session Summary

| Session | Trigger | User-facing | Primary responsibility |
|---------|---------|-------------|------------------------|
| Main agent | User message / normal conversation startup | Yes | Run product, manage tasks, handle operator actions needing richer tools |
| Heartbeat cron session | Durable `heartbeat` cron daily | Usually no; may deliver reminders | Light-touch operational health and stranded reminder delivery |
| Reminder delivery sweep | Durable `reminder-delivery-sweep` cron every 2 hours | Usually no; may deliver reminders | Narrow idle-session reminder handoff delivery |
| Janitor cron session | Durable `janitor` cron weekly | No; may alert operator | Deep operational audit and cron drift correction |
| Isolated cron session | Durable cron schedule in `setup/cron/` | No | Script-first background work, narrow scope |

## Main Agent

Primary OpenClaw session. User-facing conversation. Loads repo bootstrap files (`AGENTS.md`, `SOUL.md`, `USER.md`, `MEMORY.md`, `TOOLS.md`, related docs), runs intent flow, owns product behavior.

### Confirmed tool contract

Only session with confirmed contract for higher-authority operational tools:

- CLI access to `openclaw config get`, `openclaw config set`, and `openclaw config schema` for reading/updating `~/.openclaw/openclaw.json`
- Full cron administration including manual runs and broader job management beyond startup's canonical-registration scope
- `message` for proactive outbound delivery across configured channels
- `edit` and `write` for repo files and direct workspace mutation
- Gateway lifecycle/config tools: restart, config inspection

`exec` and `read` confirmed for heartbeat and isolated cron under narrower scopes. Main agent only session assuming broader operator workflows.

### Operational responsibilities

Main agent responsible for:

- normal hide-my-list conversation loop and all task-management behavior
- startup checks in `AGENTS.md`, including opportunistic reminder delivery from handoff file on every user interaction
- startup checks in `AGENTS.md`, including canonical recurring cron existence repair
- reading `state.json.recent_outbound` on startup so terse follow-up replies can be matched to recently-sent reminders or other outbound prompts
- calling `scripts/notion-cli.sh complete-reminder PAGE_ID sent` after successful reminder delivery, then removing handoff file
- recording delivered reminders into `state.json.recent_outbound` before cleanup and clearing/resolving those entries after the user's reply is understood
- applying OpenClaw config changes requiring `openclaw config set`, including whole-subtree heartbeat config-drift repair after template changes
- operator/debugging work needing richer tools: reading logs, inspecting config, adjusting cron registrations, filing GitHub issues describing runtime failures

### Explicit boundary

If workflow needs `openclaw config get`, `openclaw config set`, or any `openclaw.json` mutation → **main-agent responsibility**. Heartbeat and isolated cron must not assume those tools.

Tool availability does not override `AGENTS.md` safety policy. External actions still require user approval. OpenClaw prompt/spec files go through GitHub issue -> PR -> review path, not direct runtime edits. Direct writes limited to `AGENTS.md` allowlist except documented dirty-pull recovery path.

## Heartbeat Cron Session

Short durable cron session configured in `setup/cron/heartbeat.md`. Runs daily as an isolated cheap-tier session and reads `docs/heartbeat-checks.md` as the authoritative check list. Built-in OpenClaw heartbeat is disabled in `setup/openclaw.json.template` with `agents.defaults.heartbeat.every: "0s"`.

### Confirmed tool contract

Narrower confirmed contract:

- `exec` and `read` for script execution and repo inspection
- `message` for explicit reminder delivery to Signal from heartbeat Check 1 (`docs/heartbeat-checks.md`), and for ops-alert messages to the Signal recipient heartbeat resolves from `OPS_ALERT_SIGNAL_NUMBER` when critical failures require operator attention (malformed reminder handoff, Notion connectivity failure, outbound media permission failure, persistent dirty-pull recovery failure)
- CronList and CronCreate for durable cron inspection and missing-job re-registration defined in `docs/heartbeat-checks.md`

### Do not assume

Treat these as unconfirmed for heartbeat cron sessions:

- `openclaw config get`, `openclaw config set`, `openclaw config schema`
- broader proactive `message` workflows beyond explicit reminder delivery and confirmed ops alerts to Signal
- CronUpdate/CronDelete drift correction; weekly janitor owns that path unless heartbeat support is explicitly re-expanded and documented
- gateway lifecycle tools
- general repo-edit/write as routine heartbeat behavior

Until explicitly confirmed, heartbeat instructions must not depend on them.

### Operational responsibilities

Heartbeat responsible for:

- reading reminder handoff file and delivering stranded reminders as daily backstop
- recording delivered reminder context in `state.json.recent_outbound` so later sessions can understand short replies
- completing delivered reminders in Notion with `scripts/notion-cli.sh complete-reminder PAGE_ID sent`
- verifying durable cron jobs exist in OpenClaw
- checking Notion connectivity
- verifying outbound media staging permissions for Signal attachments
- retrying dirty-pull recovery when `.pull-dirty` indicates isolated cron could not finish recovery path

### Explicit boundary

Heartbeat cron = **operations backstop**, not primary control plane. Keeps existing system healthy. Not where repo docs assume config mutation, broad gateway control, or user-conversation logic. If the `heartbeat` cron itself is deleted entirely, AGENTS.md startup checks restore it from `setup/cron/heartbeat.md` on the next user interaction.

## Reminder Delivery Sweep

Short durable cron session configured in `setup/cron/reminder-delivery-sweep.md`. Runs every 2 hours as an isolated cheap-tier session and executes only Check 1 from `docs/heartbeat-checks.md`.

### Operational responsibilities

Reminder delivery sweep responsible for:

- reading reminder handoff file and delivering stranded reminders as the idle-session backstop
- recording delivered reminder context in `state.json.recent_outbound`
- completing delivered reminders in Notion with `scripts/notion-cli.sh complete-reminder PAGE_ID sent`

### Explicit boundary

Reminder delivery sweep does not run cron registration repair, drift correction, Notion connectivity checks, dirty-pull recovery, or janitor audits. It exists only to keep fallback reminder delivery on a short cadence while heartbeat stays daily.

## Janitor Cron Session

Weekly durable cron session configured in `setup/cron/janitor.md`. Runs as an isolated Opus session with `payload.lightContext: false`, so it receives full bootstrap context for reasoning-heavy cleanup.

### Confirmed tool contract

Janitor uses the same narrow operational tools as heartbeat where documented:

- `exec` and `read` for script execution and repo inspection
- `message` for concise ops-alert summaries to the Signal recipient resolved from `OPS_ALERT_SIGNAL_NUMBER`
- CronList, CronCreate, CronUpdate, CronDelete for recurring cron drift correction and stale-job cleanup defined in `docs/heartbeat-checks.md`

### Operational responsibilities

Janitor responsible for:

- comparing canonical recurring cron specs against live registrations and patching drift
- deeper environment and secrets sanity checks without printing secret values
- Notion/state audits for stale reminders, missing recurring-task follow-ups, and expired `recent_outbound`
- memory rot checks for stale or contradictory context
- cron run-history review for rising failures or duration outliers
- sending one end-of-run ops summary only when findings are actionable

### Explicit boundary

Janitor can surface suspicious Notion or memory data but should not auto-prune records that require operator judgment. It stays silent on a clean week.

## Isolated Cron Sessions

Durable OpenClaw jobs registered from `setup/cron/` with `sessionTarget: isolated`, `payload.kind: agentTurn`, and a concrete model in the canonical cron spec. Routine jobs use `payload.lightContext: true` (empty bootstrap — prompts are self-contained scripts or spec readers) and cheap-tier model. Janitor is the exception: weekly Opus, `payload.lightContext: false`, full bootstrap. `pull-main` and `reminder-check` are intentionally shell-first and should move to shell cron payloads once OpenClaw supports them. Run background work without loading prior user conversation transcripts.

### Shared tool assumptions

Isolated cron prompts assume only what narrow script execution needs:

- `exec` and `read` for running scripts and checking simple repo state
- no direct user-delivery responsibility
- no assumption of `openclaw config get` / `openclaw config set`, gateway control, or full cron-admin authority

Every isolated cron job stays silent unless prompt explicitly requires status reply. Current jobs intentionally end with `NO_REPLY`.

### `heartbeat`

Operational-health backstop. Responsible for:

- read `docs/heartbeat-checks.md`
- deliver stranded reminder handoffs through the explicit `message` tool path
- verify canonical recurring cron registrations exist
- test Notion connectivity
- verify outbound media staging permissions for Signal attachments
- retry dirty-pull recovery when `.pull-dirty` persists

Not responsible for:

- mutating `openclaw.json` through `openclaw config set`
- full cron drift correction
- normal user-conversation flow
- broad gateway administration

### `janitor`

Weekly deep audit. Responsible for:

- read `docs/heartbeat-checks.md`
- compare live recurring cron jobs to canonical `setup/cron/` specs and patch drift
- audit environment, Notion/state consistency, memory rot, and cron run history
- send one ops-alert summary only when findings are actionable

Not responsible for:

- user-facing reminder delivery
- broad gateway administration
- auto-pruning Notion or memory data that requires operator judgment

### `reminder-check`

Query-only. Responsible for:

- run `scripts/check-reminders.sh`
- discover due reminders
- write repo-root reminder handoff file for another session to deliver later

Not responsible for:

- sending reminder to user
- calling `complete-reminder`
- deleting handoff file after discovery

### `pull-main`

Workspace-maintenance only. Responsible for:

- run `scripts/pull-main.sh`
- keep local checkout aligned with `origin/main`
- let script handle dirty-pull recovery including GitHub issue creation when needed

Not responsible for:

- reapplying cron specs after pull
- patching OpenClaw config
- handling user-facing messaging

## Operational Split That Other Docs Should Follow

Keep these boundaries intact when writing or reviewing runtime docs:

- **Conversation and config mutation:** main agent
- **Daily light-touch health checks and stranded reminder delivery:** heartbeat cron
- **Weekly deep audit and cron drift correction:** janitor cron
- **Cheap background polling or sync work:** routine isolated cron sessions

That means:

- reminder discovery in isolated cron; primary reminder delivery via one-shot `reminder-<page_id>` cron registered at intake; startup path + heartbeat = safety net only
- recurring cron existence repair belongs to startup + heartbeat; full drift correction belongs to janitor, not script-only cron jobs
- `openclaw.json` drift repair belongs to main agent unless heartbeat config tool access explicitly confirmed

If platform changes and new tools become available to heartbeat or isolated cron, update this document first, then update `docs/heartbeat-checks.md` or `setup/cron/` to rely on new contract.
