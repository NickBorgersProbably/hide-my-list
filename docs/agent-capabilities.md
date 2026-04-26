# Agent Capabilities and Operational Boundaries

Defines runtime roles in hide-my-list's OpenClaw deployment. Answers which session does which work.

Use as source of truth when updating `AGENTS.md`, `HEARTBEAT.md`, `docs/heartbeat-checks.md`, `setup/cron/`, or any operational doc assuming tool contract.

## Why This Exists

hide-my-list runs multiple OpenClaw session types:

- **main agent** — user talks to
- built-in **heartbeat** session runs `HEARTBEAT.md` (stub; reads full checks from `docs/heartbeat-checks.md`)
- isolated **durable cron sessions** like `reminder-check` and `pull-main`

Sessions have different responsibilities and different tools. Config patching belongs to main agent unless another session's access explicitly confirmed.

## Session Summary

| Session | Trigger | User-facing | Primary responsibility |
|---------|---------|-------------|------------------------|
| Main agent | User message / normal conversation startup | Yes | Run product, manage tasks, handle operator actions needing richer tools |
| Heartbeat session | Built-in OpenClaw heartbeat every 60 minutes | Usually no; may deliver reminders | Backstop operational health and stranded reminder delivery |
| Isolated cron session | Durable cron schedule in `setup/cron/` | No | Cheap script-first background work, narrow scope |

## Main Agent

Primary OpenClaw session. User-facing conversation. Loads repo bootstrap files (`AGENTS.md`, `SOUL.md`, `USER.md`, `MEMORY.md`, `TOOLS.md`, related docs), runs intent flow, owns product behavior.

### Confirmed tool contract

Only session with confirmed contract for higher-authority operational tools:

- `config.get`, `config.patch`, `config.schema.lookup` for reading/patching `~/.openclaw/openclaw.json`
- Full cron administration including manual runs and broader job management beyond heartbeat's drift-correction scope
- `message` for proactive outbound delivery across configured channels
- `edit` and `write` for repo files and direct workspace mutation
- Gateway lifecycle/config tools: restart, config inspection

`exec` and `read` confirmed for heartbeat and isolated cron under narrower scopes. Main agent only session assuming broader operator workflows.

### Operational responsibilities

Main agent responsible for:

- normal hide-my-list conversation loop and all task-management behavior
- startup checks in `AGENTS.md`, including opportunistic reminder delivery from handoff file on every user interaction
- reading `state.json.recent_outbound` on startup so terse follow-up replies can be matched to recently-sent reminders or other outbound prompts
- calling `scripts/notion-cli.sh complete-reminder PAGE_ID sent|missed` after successful reminder delivery, then removing handoff file
- recording delivered reminders into `state.json.recent_outbound` before cleanup and clearing/resolving those entries after the user's reply is understood
- applying OpenClaw config changes requiring `config.patch`, including config-drift repair after template changes
- operator/debugging work needing richer tools: reading logs, inspecting config, adjusting cron registrations, filing GitHub issues describing runtime failures

### Explicit boundary

If workflow needs `config.get`, `config.patch`, or any `openclaw.json` mutation → **main-agent responsibility**. Heartbeat and isolated cron must not assume those tools.

Tool availability does not override `AGENTS.md` safety policy. External actions still require user approval. OpenClaw prompt/spec files go through GitHub issue -> PR -> review path, not direct runtime edits. Direct writes limited to `AGENTS.md` allowlist except documented dirty-pull recovery path.

## Heartbeat Session

Short built-in OpenClaw session configured in `openclaw.json`, driven by `HEARTBEAT.md` (bootstrap stub that delegates to `docs/heartbeat-checks.md`). Runs every 60 minutes with cheap-tier model (`modelTiers.cheap`).

### Confirmed tool contract

Narrower confirmed contract:

- `exec` and `read` for script execution and repo inspection
- `message` for explicit reminder delivery to Signal from heartbeat Check 1 (`docs/heartbeat-checks.md`), and for ops-alert messages to the Signal recipient heartbeat resolves from `OPS_ALERT_SIGNAL_NUMBER` when critical failures require operator attention (malformed reminder handoff, Notion connectivity failure, persistent dirty-pull recovery failure)
- CronList, CronCreate, CronUpdate, CronDelete for durable cron inspection, re-registration, drift correction, stale-job cleanup defined in `docs/heartbeat-checks.md`

### Do not assume

Treat these as unconfirmed for heartbeat sessions:

- `config.get`, `config.patch`, `config.schema.lookup`
- broader proactive `message` workflows beyond explicit reminder delivery and confirmed ops alerts to Signal
- gateway lifecycle tools
- general repo-edit/write as routine heartbeat behavior

Until explicitly confirmed, heartbeat instructions must not depend on them.

### Operational responsibilities

Heartbeat responsible for:

- reading reminder handoff file and delivering stranded reminders as hourly backstop
- recording delivered reminder context in `state.json.recent_outbound` so later sessions can understand short replies
- completing delivered reminders in Notion with `scripts/notion-cli.sh complete-reminder PAGE_ID sent|missed`
- verifying durable cron jobs exist and match canonical specs in `setup/cron/`
- checking Notion connectivity and basic environment health
- retrying dirty-pull recovery when `.pull-dirty` indicates isolated cron could not finish recovery path

### Explicit boundary

Heartbeat = **operations backstop**, not primary control plane. Keeps existing system healthy. Not where repo docs assume config mutation, broad gateway control, or user-conversation logic.

## Isolated Cron Sessions

Durable OpenClaw jobs registered from `setup/cron/` with `sessionTarget: isolated`, `payload.kind: agentTurn`, `payload.lightContext: true` (empty bootstrap — prompts are self-contained scripts), lightweight model. Run cheap background work without loading main conversational context.

### Shared tool assumptions

Isolated cron prompts assume only what narrow script execution needs:

- `exec` and `read` for running scripts and checking simple repo state
- no direct user-delivery responsibility
- no assumption of `config.patch`, gateway control, or full cron-admin authority

Every isolated cron job stays silent unless prompt explicitly requires status reply. Current jobs intentionally end with `NO_REPLY`.

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
- **Hourly health checks and stranded reminder delivery:** heartbeat
- **Cheap background polling or sync work:** isolated cron sessions

That means:

- reminder discovery in isolated cron; reminder delivery stays with main agent startup path or heartbeat
- cron drift correction belongs to heartbeat, not isolated cron jobs
- `openclaw.json` drift repair belongs to main agent unless heartbeat config tool access explicitly confirmed

If platform changes and new tools become available to heartbeat or isolated cron, update this document first, then update `docs/heartbeat-checks.md` or `setup/cron/` to rely on new contract.
