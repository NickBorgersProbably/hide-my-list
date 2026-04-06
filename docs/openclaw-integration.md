# How hide-my-list Maps to OpenClaw

This document explains how hide-my-list's design leverages (and doesn't leverage) OpenClaw's architecture. It's meant to help contributors understand which pieces are ours, which are OpenClaw's, and where the boundaries are.

## The Core Idea

hide-my-list has no server, no UI framework, no database ORM. The entire application is a set of markdown files that an AI agent reads and follows. OpenClaw is the runtime that makes this work — it provides the agent session, messaging channels, scheduling, and lifecycle management.

This repo *is* the OpenClaw workspace. When deployed, the repo root sits at `~/.openclaw/workspace/` and OpenClaw's agent reads our files directly.

## Workspace Bootstrap Files

OpenClaw has a concept of "bootstrap files" — markdown files at the workspace root that the agent loads at session start. These are the recognized basenames:

| File | OpenClaw Role | Our Usage |
|------|--------------|-----------|
| `AGENTS.md` | Primary operational instructions | Full agent runbook: startup sequence, intent detection, Notion CRUD, state management, doc-read requirements |
| `SOUL.md` | Agent personality/identity | Personality definition, the "never show the list" rule, tone guidelines, ADHD research foundation |
| `IDENTITY.md` | Agent metadata (name, vibe) | Name, creature description, emoji policy |
| `USER.md` | Context about the human | Per-user: name, timezone, preferences. Gitignored; created from template |
| `MEMORY.md` | Long-term memory/lessons | Per-user: learned preferences, hard rules, system behaviors. Gitignored; created from template |
| `TOOLS.md` | Local tool documentation | Notion property names, status values, state file reference |
| `HEARTBEAT.md` | Periodic health check instructions | Cron job re-registration and drift correction, Notion connectivity, environment checks |

OpenClaw loads these automatically via the `bootstrap-extra-files` hook. We don't need any special configuration for the agent to find them — just having them at the workspace root is enough.

**What we don't use:** `BOOT.md` (one-time gateway startup script). We could use this to auto-register cron jobs, but currently the heartbeat handles re-registration and `setup/bootstrap.sh` handles initial setup.

## Heartbeat

OpenClaw's heartbeat is a built-in periodic trigger configured in `openclaw.json`:

```json
"heartbeat": {
  "every": "60m",
  "model": "litellm/claude-sonnet-4-6"
}
```

Every 60 minutes, OpenClaw creates a short agent session that reads `HEARTBEAT.md` and executes the checks defined there. It uses a lighter model (Sonnet instead of Opus) since these are routine operational tasks.

**Our usage:** We use the heartbeat as a safety net for the cron system. Its primary job is to verify that durable cron jobs still match the canonical `CronCreate` specs in `setup/cron/`: if a job expired, heartbeat re-registers it; if a live job drifted from its spec, heartbeat patches it back into compliance. The comparison covers the full effective registration contract, including `name`, `durable`, `schedule`, `prompt`, `sessionTarget` (when required), `model` (when required), the absence of any direct-delivery `to`, `payload.kind`, delivery behavior (`delivery.mode` or equivalent `best-effort-deliver`), and `timeout-seconds`. `HEARTBEAT.md` is the authoritative comparison checklist. The `pull-main` cron now handles the fast path too: after a clean pull that changes files in `setup/cron/`, it immediately re-applies the affected live jobs so prompt, schedule, and model fixes do not sit dormant until the next heartbeat window. Heartbeat still checks Notion connectivity and environment health. Production deployments should treat this as hourly infrastructure hygiene, not the mechanism that keeps reminders timely.

**What changed:** The heartbeat used to babysit bash daemons (checking PID files, restarting dead processes). With cron replacing daemons, it now verifies that durable cron registrations both exist and still match their specs — a much cleaner responsibility than process management.

## Managed Content Boundary

The repo-level "GitHub-only for managed content" rule is intentionally narrower than "disable OpenClaw features." It applies to normal user-requested changes to tracked product files such as prompts, docs, scripts, and design artifacts.

OpenClaw runtime features still operate normally:
- Bootstrap files are injected into session context by OpenClaw's bootstrap flow and hooks.
- Heartbeat and durable cron still run on their normal schedules.
- Cron registrations and task records live in OpenClaw-owned runtime state outside this repo checkout.
- Messaging, session lifecycle, and hooks remain platform responsibilities.

The one repo-mutating runtime exception is dirty-pull recovery in `scripts/pull-main.sh`: preserve the local diff in a GitHub issue, then reset the workspace to match remote so the GitHub-reviewed branch stays the source of truth. The normal `pull-main` cron path is script-managed; `HEARTBEAT.md` remains the retry backstop if `.pull-dirty` persists after an auth or recovery failure. That exception exists to reduce merge conflicts and recover safely, not to bypass the GitHub process.

## Cron (Durable Scheduled Jobs)

OpenClaw provides `CronCreate` for scheduling recurring agent prompts. With `durable: true`, jobs persist to disk and survive gateway restarts.

**Our usage:** Two durable cron jobs replace our former bash daemons/manual sync steps:

| Job | Schedule | Replaces |
|-----|----------|----------|
| `reminder-check` | `*/15 * * * *` | `reminder-daemon.sh` (bash while-loop) |
| `pull-main` | `*/10 * * * *` | Manual `git pull origin main` hygiene plus immediate re-application of changed `setup/cron/` specs after a clean pull |

**Why this is better than daemons:**
- No PID files, no silent death, no orphaned processes
- OpenClaw manages the scheduling; failures are visible in the session
- Reminder delivery still happens in agent context, but `scripts/check-reminders.sh` hands due reminders to the cron prompt through `.reminder-signal` instead of relying on a long-running daemon
- Cron only fires when the REPL is idle, which is actually better for ADHD — it won't interrupt the user mid-task

**The 7-day expiry problem:** Recurring cron jobs auto-expire after 7 days. The heartbeat catches this and re-registers the missing jobs. It also corrects spec drift caused by manual hotfixes, failed pull-time re-application, or stale re-registration prompts by comparing the live job against the canonical `CronCreate` block and patching any mismatched registration fields. This is a platform constraint we work around rather than a feature we chose.

**Current registration contract:** `reminder-check` re-enters the bound `main` session with `payload.kind: systemEvent`, `delivery.mode: none`, and `timeout-seconds: 120` so reminder delivery stays pinned to the existing user-facing surface. `pull-main` runs as an isolated cron turn with `sessionTarget: isolated`, `model: litellm/claude-haiku-4-5`, `payload.kind: agentTurn`, `delivery.mode: none`, and `timeout-seconds: 120` so routine sync work stays off the main Opus/Sonnet conversation session. `pull-main` patches changed cron jobs in place after a clean pull by comparing the before/after `HEAD` commits from that invocation, reading the canonical spec files, and calling `CronUpdate` on the affected live registrations. The cron prompts should end with an explicit `NO_REPLY` instruction so routine checks stay silent unless there is something actionable.

### Production Timing Recommendation

For production deployments, use these timings unless you have a clear reason to pay for tighter polling:

| Mechanism | Recommended cadence | Why |
|-----------|---------------------|-----|
| Heartbeat | Every 60 minutes | Infrastructure safety net only: cron expiry, spec drift, Notion/env health |
| `reminder-check` | Every 15 minutes | User-facing reminder timeliness with acceptable cost for routine reminders on a mostly-idle job |
| `pull-main` | Every 10 minutes | Cheap script-only sync path; keeps the workspace fresh |

The core principle is simple: `reminder-check` is the thing that affects user-facing timeliness. Heartbeat exists to keep the runtime healthy and recover expired or drifted cron registrations, so it does not need sub-hour cadence. For routine reminders, 15-minute polling is the default production cost/latency tradeoff. For exact-time reminders such as medication, departures, or meetings, tighten the polling interval rather than treating a 15-minute window as exact.

## Messaging Channels

OpenClaw handles all messaging infrastructure. We don't touch it.

```json
"channels": {
  "signal": {
    "enabled": true,
    "account": "+18883431161",
    "cliPath": "signal-cli",
    "dmPolicy": "pairing",
    "groupPolicy": "allowlist"
  }
}
```

The primary deployed surface today is Signal. OpenClaw handles:
- Message routing (inbound Signal → agent session)
- Response delivery (agent output → Signal message)
- Acknowledgment reactions
- Session scoping (per-channel-peer)

**Our role:** Zero for transport mechanics. We write conversational responses; OpenClaw delivers them. Interactive conversations use the normal main-agent routing path, reminder delivery reuses that same bound surface through `sessionTarget: main`, and routine workspace-sync maintenance runs in isolated Haiku turns that still end in `NO_REPLY` unless something requires operator attention.

## Model Routing (LiteLLM Proxy)

OpenClaw supports multiple model providers. We route through a LiteLLM proxy on the Tailscale network:

```json
"models": {
  "providers": {
    "litellm": {
      "baseUrl": "https://llm.featherback-mermaid.ts.net/v1",
      "models": [
        { "id": "claude-opus-4-6", ... },
        { "id": "claude-sonnet-4-6", ... },
        { "id": "claude-haiku-4-5", ... }
      ]
    }
  }
}
```

- **Primary model:** Claude Opus 4.6 (conversations, task management)
- **Heartbeat model:** Claude Sonnet 4.6 (routine checks, cheaper)
- **Cron maintenance model:** Claude Haiku 4.5 (`pull-main` isolated sync turns)
- **Fallback chain:** Opus → Sonnet → GPT-5.4

**Our role:** We don't interact with model selection directly. The prompts in `docs/ai-prompts.md` are model-agnostic. OpenClaw picks the model based on the config.

## Gateway

The OpenClaw gateway is a WebSocket server that manages agent sessions, channel routing, and the control UI.

```json
"gateway": {
  "port": 18789,
  "mode": "local",
  "bind": "loopback"
}
```

**Our role:** Zero direct interaction. The gateway is infrastructure. We care about it only in two ways:
1. It must be running for the agent to work (`openclaw gateway`)
2. The `controlUi.allowedOrigins` setting controls who can access the web management interface

## Tool-Use Hooks (.claude/settings.json)

OpenClaw's agent sessions are built on Claude Code's REPL. This means Claude Code's hook system works inside OpenClaw — `.claude/settings.json` at the project level is respected.

We use a `PostToolUse` hook to enforce reminder confirmation:

```json
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "Bash",
      "hooks": [{
        "type": "command",
        "command": "if echo \"$TOOL_INPUT\" | grep -q 'create-reminder'; then echo 'HOOK: Confirm reminder details to user.'; fi",
        "timeout": 3000
      }]
    }]
  }
}
```

This is a Claude Code mechanism, not an OpenClaw one. OpenClaw has its own hook system (`openclaw hooks`) for platform-level events like `agent:bootstrap`, but for tool-use triggers we use the Claude Code layer.

**Important distinction:**
- **OpenClaw hooks** (`openclaw hooks list`): Platform events — bootstrap, session-memory, command-logging. Configured in `openclaw.json`.
- **Claude Code hooks** (`.claude/settings.json`): Tool-use events — PostToolUse, PreToolUse. Configured per-project.

## OpenClaw Hooks (Platform-Level)

OpenClaw has four bundled hooks. We use the defaults:

| Hook | What it does | Our config |
|------|-------------|------------|
| `boot-md` | Runs BOOT.md on gateway startup | Default (we don't have a BOOT.md) |
| `bootstrap-extra-files` | Loads extra workspace files at session start | Default (loads SOUL.md, USER.md, etc. from workspace root) |
| `command-logger` | Logs commands to audit file | Default |
| `session-memory` | Saves context on /new or /reset | Default |

We haven't needed custom OpenClaw hooks yet.

## Skills

OpenClaw has a skills system (53 bundled, custom installable). Skills are user-invocable slash commands.

**Our usage:** None for the core application. The ADHD-informed design intentionally avoids structured commands — the user speaks naturally and the agent detects intent. Adding a `/add-task` skill would bypass the inference-first, shame-safe intake flow that is core to the design.

**Where skills could help (future):**
- An `/admin` skill for operator diagnostics (not the end user)
- A `/weekly-recap` skill wrapping `scripts/generate-weekly-recap.sh`

**Installed skills on the current instance:** `wifi-diagnostics` (unrelated to hide-my-list, installed for the home-automation project).

## Sub-agents

OpenClaw supports up to 8 concurrent sub-agents.

**Our usage:** Not directly. The PR review pipeline (GitHub Actions) uses Codex CLI agents for multi-perspective code review, but that runs in CI, not in the OpenClaw instance. The conversational agent itself is single-threaded by design — ADHD users benefit from one conversation flow, not parallel threads.

## What We Own vs. What OpenClaw Owns

```
┌─────────────────────────────────────────────────┐
│                  OpenClaw Platform               │
│                                                  │
│  Gateway ─── Channel Routing ─── Model Proxy     │
│     │              │                  │          │
│  Sessions    Signal/Web/etc    LiteLLM/Anthropic │
│     │                                            │
│  Heartbeat ── Cron ── Hooks ── Skills            │
│                                                  │
├─────────────────────────────────────────────────┤
│                                                  │
│              hide-my-list (this repo)            │
│                                                  │
│  SOUL.md ── AGENTS.md ── HEARTBEAT.md            │
│       (personality)  (operations)  (health)      │
│                                                  │
│  docs/           scripts/          state.json    │
│  (behavior spec) (Notion CRUD)    (runtime)      │
│                                                  │
│  .claude/settings.json                           │
│  (tool-use hooks)                                │
���                                                  │
│  setup/                                          │
│  (deployment templates & cron definitions)       │
│                                                  │
└─���───────────────────────────��───────────────────┘
```

**We own:** What the agent knows, how it behaves, what it stores, and how it checks its own health.

**OpenClaw owns:** How messages arrive, how models are called, how sessions persist, and how scheduling runs.

The boundary is clean: we write markdown and bash scripts; OpenClaw turns that into a running agent that talks to people.
