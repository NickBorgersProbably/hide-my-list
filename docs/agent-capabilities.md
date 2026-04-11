# Agent Capabilities and Operational Boundaries

This document defines the runtime roles inside hide-my-list's OpenClaw deployment. It exists to answer a specific question that the other docs only implied: which session is allowed to do which kind of work.

Use this as the source of truth when updating `AGENTS.md`, `HEARTBEAT.md`, `setup/cron/`, or any operational doc that assumes a tool contract.

## Why This Exists

hide-my-list runs through multiple OpenClaw session types:

- the **main agent** the user talks to
- the built-in **heartbeat** session that runs `HEARTBEAT.md`
- isolated **durable cron sessions** such as `reminder-check` and `pull-main`

Those sessions do not have the same responsibilities, and they should not be assumed to have the same tools. In particular, config patching belongs to the main agent unless another session's access is explicitly confirmed.

## Session Summary

| Session | Trigger | User-facing | Primary responsibility |
|---------|---------|-------------|------------------------|
| Main agent | User message / normal conversation startup | Yes | Run the product, manage tasks, handle operator actions that need richer tools |
| Heartbeat session | Built-in OpenClaw heartbeat every 60 minutes | Usually no; may deliver reminders | Backstop operational health and stranded reminder delivery |
| Isolated cron session | Durable cron schedule in `setup/cron/` | No | Cheap script-first background work with narrow scope |

## Main Agent

The main agent is the primary OpenClaw session for hide-my-list. It is the conversation the user actually experiences. It loads the repo bootstrap files (`AGENTS.md`, `SOUL.md`, `USER.md`, `MEMORY.md`, `TOOLS.md`, and related docs), runs the intent flow, and owns the product behavior.

### Confirmed tool contract

The main agent is the only session type with a confirmed contract for the following higher-authority operational tools:

- `config.get`, `config.patch`, `config.schema.lookup` for reading and patching `~/.openclaw/openclaw.json`
- Full cron administration, including manual runs and broader job management beyond heartbeat's narrower drift-correction scope
- `message` for proactive outbound delivery across configured channels
- `exec`, `read`, `edit`, `write` for repo files, logs, and scripts
- Gateway lifecycle/config tools such as restart and config inspection

### Operational responsibilities

The main agent is responsible for:

- running the normal hide-my-list conversation loop and all task-management behavior
- performing the startup checks in `AGENTS.md`, including opportunistic reminder delivery from the handoff file on every user interaction
- calling `scripts/notion-cli.sh complete-reminder PAGE_ID sent|missed` after successful reminder delivery, then removing the handoff file
- applying OpenClaw config changes that require `config.patch`, including config-drift repair after template changes
- handling operator/debugging work that depends on richer tools, such as reading logs, inspecting config, adjusting cron registrations, or filing GitHub issues that describe runtime failures

### Explicit boundary

If a workflow needs `config.get`, `config.patch`, or any other `openclaw.json` mutation, treat that as a **main-agent responsibility**. Heartbeat and isolated cron sessions must not be assumed to have those tools.

Tool availability does not override `AGENTS.md` safety policy. External actions still require user approval, OpenClaw prompt/spec files still go through the GitHub issue -> PR -> review path instead of direct runtime edits, and direct writes remain limited to the `AGENTS.md` allowlist except for the documented dirty-pull recovery path.

## Heartbeat Session

The heartbeat is a short built-in OpenClaw session configured in `openclaw.json` and driven by `HEARTBEAT.md`. In this repo it runs every 60 minutes with a lighter model than the main agent.

### Confirmed tool contract

The heartbeat session has a narrower confirmed contract:

- `exec` and `read` for script execution and repo inspection
- Cron registration tools used by `HEARTBEAT.md` for drift correction and re-registration, specifically the ability to inspect and patch durable cron jobs

### Do not assume

The repo should currently treat these capabilities as unconfirmed for heartbeat sessions:

- `config.get`, `config.patch`, `config.schema.lookup`
- proactive `message` tooling outside normal heartbeat output routing
- gateway lifecycle tools
- general repo-edit/write capabilities as part of routine heartbeat behavior

Until those are explicitly confirmed, heartbeat instructions must not depend on them.

### Operational responsibilities

The heartbeat is responsible for:

- reading the reminder handoff file and delivering stranded reminders as the hourly backstop
- completing delivered reminders in Notion with `scripts/notion-cli.sh complete-reminder PAGE_ID sent|missed`
- verifying that durable cron jobs still exist and still match the canonical specs in `setup/cron/`
- checking Notion connectivity and basic environment health
- retrying dirty-pull recovery when `.pull-dirty` indicates the isolated cron could not finish the recovery path

### Explicit boundary

Heartbeat is an **operations backstop**, not the primary control plane. It should keep the existing system healthy, but it should not be the place where repo docs assume config mutation, broad gateway control, or user-conversation logic.

## Isolated Cron Sessions

Isolated cron sessions are durable OpenClaw jobs registered from `setup/cron/` with `sessionTarget: isolated`, `payload.kind: agentTurn`, and a lightweight model. They exist to run cheap background work without loading the main conversational context.

### Shared tool assumptions

Isolated cron prompts should assume only what they need for narrow script execution:

- `exec` and `read` for running scripts and checking simple repo state
- no direct user-delivery responsibility
- no assumption of `config.patch`, gateway control, or full cron-admin authority

Every isolated cron job should stay silent unless its prompt explicitly requires a status reply, and the current jobs intentionally end with `NO_REPLY`.

### `reminder-check`

`reminder-check` is query-only. Its responsibility is:

- run `scripts/check-reminders.sh`
- discover due reminders
- write the repo-root reminder handoff file for another session to deliver later

It is not responsible for:

- sending the reminder to the user
- calling `complete-reminder`
- deleting the handoff file after discovery

### `pull-main`

`pull-main` is workspace-maintenance only. Its responsibility is:

- run `scripts/pull-main.sh`
- keep the local checkout aligned with `origin/main`
- let the script handle dirty-pull recovery, including GitHub issue creation when needed

It is not responsible for:

- reapplying cron specs after a pull
- patching OpenClaw config
- handling user-facing messaging

## Operational Split That Other Docs Should Follow

When writing or reviewing runtime docs, keep these boundaries intact:

- **Conversation and config mutation:** main agent
- **Hourly health checks and stranded reminder delivery:** heartbeat
- **Cheap background polling or sync work:** isolated cron sessions

That means:

- reminder discovery can happen in isolated cron, but reminder delivery stays with the main agent startup path or heartbeat
- cron drift correction belongs to heartbeat, not the isolated cron jobs themselves
- `openclaw.json` drift repair belongs to the main agent unless heartbeat access to config tools is explicitly confirmed later

If the platform changes and new tools become available to heartbeat or isolated cron sessions, update this document first, then update `HEARTBEAT.md` or `setup/cron/` to rely on that new contract.
