# How hide-my-list Maps to OpenClaw

Explains which pieces ours, which OpenClaw's, where boundaries are.

For session-by-session tool contract and ownership split between main agent, heartbeat, isolated cron jobs, see [Agent Capabilities](agent-capabilities.md).

## The Core Idea

hide-my-list: no server, no UI framework, no database ORM. Entire app = markdown files AI agent reads and follows. OpenClaw = runtime — provides agent session, messaging, scheduling, lifecycle.

Repo *is* OpenClaw workspace. Deployed: repo root sits at `~/.openclaw/workspace/`, OpenClaw agent reads files directly.

## Workspace Bootstrap Files

OpenClaw "bootstrap files" = markdown at workspace root, loaded at session start. Recognized basenames:

| File | OpenClaw Role | Our Usage |
|------|--------------|-----------|
| `AGENTS.md` | Primary operational instructions | Full agent runbook: startup sequence, intent detection, Notion CRUD, state management, doc-read requirements |
| `SOUL.md` | Agent personality/identity | Personality definition, "never show the list" rule, tone guidelines, ADHD research foundation |
| `IDENTITY.md` | Agent metadata (name, vibe) | Name, creature description, emoji policy |
| `USER.md` | Context about the human | Per-user: name, timezone, preferences. Gitignored; created from template |
| `MEMORY.md` | Long-term memory/lessons | Per-user: learned preferences, hard rules, system behaviors. Gitignored; created from template |
| `TOOLS.md` | Local tool documentation | Notion property names, status values, state file reference |
| `HEARTBEAT.md` | Periodic health check instructions | Stub that delegates to `docs/heartbeat-checks.md` — keeps bootstrap payload small while heartbeat session reads the full check list on demand |

OpenClaw loads via `bootstrap-extra-files` hook automatically. No special config needed — workspace root placement enough.

**What we don't use:** `BOOT.md` (one-time gateway startup script). Could auto-register cron jobs, but heartbeat handles re-registration and `setup/bootstrap.sh` handles initial setup.

## Heartbeat

OpenClaw heartbeat = built-in periodic trigger configured in `openclaw.json`:

```json
"heartbeat": {
  "every": "60m",
  "model": "litellm/claude-sonnet-4-6"
}
```

Every 60 min, OpenClaw creates short agent session, reads `HEARTBEAT.md`, executes checks. Uses lighter model (Sonnet not Opus) — routine operational tasks. Reminder delivery not via `heartbeat.target`; Check 1 sends reminders explicitly with OpenClaw `message` tool (`action: send`, `channel: signal`). `target` field only controls where generic non-`HEARTBEAT_OK` output routes; without it, defaults to `"none"`, silently discarded ([openclaw/openclaw#29215](https://github.com/openclaw/openclaw/issues/29215)).

**Our usage:** Two roles:
1. **Reminder-delivery backstop:** Isolated `reminder-check` cron only writes `.reminder-signal` — no user delivery. Heartbeat Check 1 reads stranded signal files, validates, delivers to Signal via `message` tool every 60 min. (AGENTS.md startup check provides faster opportunistic delivery when user active.)
2. **Cron safety net:** Verify durable cron jobs still match canonical `CronCreate` specs in `setup/cron/`: expired jobs get re-registered; drifted jobs get patched back. Comparison covers full effective registration contract: `name`, `durable`, `schedule`, `prompt`, `sessionTarget`, `model`, absence of direct-delivery `to`, `payload.kind`, `timeout-seconds`. `docs/heartbeat-checks.md` = authoritative comparison checklist (HEARTBEAT.md is a bootstrap stub that delegates to it).

Heartbeat intentionally not place to assume `config.patch` access. Config mutation = main-agent responsibility unless heartbeat support explicitly confirmed and documented in [Agent Capabilities](agent-capabilities.md).

Heartbeat also checks Notion connectivity and environment health. Production: treat as hourly infrastructure hygiene.

Backstop stays in design because OpenClaw doesn't expose post-delivery acknowledgment hook for `announce`. Without that hook, announce-only cron would have to mark reminders `sent` or `missed` before platform could prove delivery succeeded — breaks durable retry if turn dies in between.

**What changed:** Heartbeat used to babysit bash daemons (PID files, restarting dead processes). With cron replacing daemons, now verifies durable cron registrations exist and match specs — cleaner than process management.

## Managed Content Boundary

Repo-level "GitHub-only for managed content" rule intentionally narrower than "disable OpenClaw features." Applies to normal user-requested changes to tracked product files: prompts, docs, scripts, design artifacts.

OpenClaw runtime features still run normally:
- Bootstrap files injected into session context by OpenClaw's bootstrap flow and hooks.
- Heartbeat and durable cron run on normal schedules.
- Cron registrations and task records live in OpenClaw-owned runtime state outside repo checkout.
- Messaging, session lifecycle, hooks = platform responsibilities.

One repo-mutating runtime exception: dirty-pull recovery in `scripts/pull-main.sh` — preserve local diff in GitHub issue, reset workspace to match remote so GitHub-reviewed branch stays source of truth. Normal `pull-main` cron path is script-managed; heartbeat (`docs/heartbeat-checks.md` Check 5) = retry backstop if `.pull-dirty` persists after auth or recovery failure. Exception exists to reduce merge conflicts and recover safely, not bypass GitHub process.

## Cron (Durable Scheduled Jobs)

OpenClaw provides `CronCreate` for recurring agent prompts. `durable: true` = jobs persist to disk, survive gateway restarts.

**Our usage:** Two durable cron jobs replace former bash daemons/manual sync steps:

| Job | Schedule | Replaces |
|-----|----------|----------|
| `reminder-check` | `*/15 * * * *` | `reminder-daemon.sh` (bash while-loop) |
| `pull-main` | `*/10 * * * *` | Manual `git pull origin main` hygiene; cron drift correction now through heartbeat |

**Why better than daemons:**
- No PID files, no silent death, no orphaned processes
- OpenClaw manages scheduling; failures visible in session
- `scripts/check-reminders.sh` writes reminder handoff file (default: `.reminder-signal`); delivery through heartbeat and main-session startup check, not cron job itself
- Cron fires only when REPL idle — better for ADHD, won't interrupt mid-task

**7-day expiry problem:** Recurring cron jobs auto-expire after 7 days. Heartbeat catches this, re-registers missing jobs. Also corrects spec drift from manual hotfixes or stale re-registration prompts by comparing live job against canonical `CronCreate` block and patching mismatched fields. Platform constraint worked around, not feature chosen.

**Current registration contract:** Both `reminder-check` and `pull-main` run as isolated Haiku sessions with `sessionTarget: isolated`, `model: litellm/claude-haiku-4-5`, `payload.kind: agentTurn`, `timeout-seconds: 60`. Deliberate: separates cheap query work from user-facing delivery. Previous architecture used `sessionTarget: main` — loaded full Opus context (~200k tokens) for routine script work. Isolated Haiku cuts per-run cost by orders of magnitude. Reminder delivery handled by heartbeat (Check 1 in `docs/heartbeat-checks.md`, every 60 min) and main-session startup check (AGENTS.md step 5, every user interaction). Fully idle worst-case delivery latency: ~75 min — up to 15 min for `reminder-check` to write handoff, then up to 60 min for heartbeat if no user interaction first. Cron prompts end with `NO_REPLY` — never produce user-facing output. Until OpenClaw exposes post-delivery acknowledgment hook, this split flow = durability boundary keeping failed deliveries retryable.

Isolated cron sessions intentionally narrow. Script runners, not substitute for main agent or heartbeat control paths. Detailed ownership split in [Agent Capabilities](agent-capabilities.md).

### Production Timing Recommendation

For production, use these timings unless clear reason to pay for tighter polling:

| Mechanism | Recommended cadence | Why |
|-----------|---------------------|-----|
| Heartbeat | Every 60 minutes | Reminder-delivery backstop plus cron expiry, spec drift, and Notion/env health |
| `reminder-check` | Every 15 minutes | Isolated Haiku query; writes `.reminder-signal` for heartbeat/startup delivery |
| `pull-main` | Every 10 minutes | Cheap script-only sync path; keeps workspace fresh |

Core principle: `reminder-check` controls when due reminders discovered; heartbeat part of idle-user delivery path. 15-min polling + hourly heartbeat = default production cost/latency tradeoff. Exact-time delivery not guaranteed in current deferred-delivery architecture; fully idle worst-case ~75 min unless user interacts sooner.

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

Primary deployed surface: Signal. OpenClaw handles:
- Message routing (inbound Signal → agent session)
- Response delivery (agent output → Signal message)
- Acknowledgment reactions
- Session scoping (per-channel-peer)

**Our role:** Zero for transport mechanics. We write conversational responses; OpenClaw delivers them. Interactive conversations use normal main-agent routing. Cron jobs = isolated Haiku sessions (query-only, no user delivery). Reminder delivery reaches user through heartbeat and main-session startup check.

## Model Routing (LiteLLM Proxy)

OpenClaw supports multiple model providers. We route through LiteLLM proxy on Tailscale network:

```json
"models": {
  "providers": {
    "litellm": {
      "baseUrl": "https://llm.featherback-mermaid.ts.net/v1",
      "models": [
        { "id": "claude-opus-4-6", ... },
        { "id": "claude-sonnet-4-6", ... },
        { "id": "claude-haiku-4-5", ... },
        { "id": "gpt-5.4", ... }
      ]
    }
  }
}
```

The `models` array above must be a strict superset of every model id referenced in the bullets below or in canonical cron/heartbeat specs (`HEARTBEAT.md`, `setup/cron/*.md`, `setup/openclaw.json.template`).

- **Primary model:** Claude Opus 4.6 (conversations, task management)
- **Heartbeat model:** Claude Sonnet 4.6 (routine checks, cheaper)
- **Cron model:** Claude Haiku 4.5 (isolated cron — reminder polling, workspace sync)
- **Fallback chain:** Opus → Sonnet → GPT-5.4 (GPT-5.4 used by Codex CLI via `.devcontainer/configure-codex.sh`; keep in sync with `.codex/config.toml`)

**Our role:** No direct interaction with model selection. Prompts in `docs/ai-prompts.md` model-agnostic. OpenClaw picks model from config.

## Gateway

OpenClaw gateway = WebSocket server managing agent sessions, channel routing, control UI.

```json
"gateway": {
  "port": 18789,
  "mode": "local",
  "bind": "loopback"
}
```

**Our role:** Zero direct interaction. Gateway = infrastructure. We care only two ways:
1. Must be running for agent to work (`openclaw gateway`)
2. `controlUi.allowedOrigins` controls who can access web management interface

## Tool-Use Hooks (.claude/settings.json)

OpenClaw agent sessions built on Claude Code REPL. Claude Code hook system works inside OpenClaw — `.claude/settings.json` at project level respected.

We use `PostToolUse` hook to enforce reminder confirmation:

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

Claude Code mechanism, not OpenClaw. OpenClaw has own hook system (`openclaw hooks`) for platform-level events like `agent:bootstrap`, but for tool-use triggers we use Claude Code layer.

**Key distinction:**
- **OpenClaw hooks** (`openclaw hooks list`): Platform events — bootstrap, session-memory, command-logging. Configured in `openclaw.json`.
- **Claude Code hooks** (`.claude/settings.json`): Tool-use events — PostToolUse, PreToolUse. Configured per-project.

## OpenClaw Hooks (Platform-Level)

OpenClaw has four bundled hooks. We use defaults:

| Hook | What it does | Our config |
|------|-------------|------------|
| `boot-md` | Runs BOOT.md on gateway startup | Default (we don't have a BOOT.md) |
| `bootstrap-extra-files` | Loads extra workspace files at session start | Default (loads SOUL.md, USER.md, etc. from workspace root) |
| `command-logger` | Logs commands to audit file | Default |
| `session-memory` | Saves context on /new or /reset | Default |

No custom OpenClaw hooks needed yet.

## Skills

OpenClaw has skills system (53 bundled, custom installable). Skills = user-invocable slash commands.

**Our usage:** None for core application. ADHD-informed design intentionally avoids structured commands — user speaks naturally, agent detects intent. Adding `/add-task` skill would bypass inference-first, shame-safe intake flow core to design.

**Where skills could help (future):**
- `/admin` skill for operator diagnostics (not end user)
- `/weekly-recap` skill wrapping `scripts/generate-weekly-recap.sh`

**Installed skills on current instance:** `wifi-diagnostics` (unrelated to hide-my-list, installed for home-automation project).

## Sub-agents

OpenClaw supports up to 8 concurrent sub-agents.

**Our usage:** Not directly. PR review pipeline (GitHub Actions) uses Codex CLI agents for multi-perspective code review — runs in CI, not OpenClaw instance. Conversational agent intentionally single-threaded — ADHD users benefit from one conversation flow, not parallel threads.

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
│  (mixed docs)    (Notion CRUD)    (runtime)      │
│                                                  │
│  .claude/settings.json                           │
│  (tool-use hooks)                                │
���                                                  │
│  setup/                                          │
│  (deployment templates & cron definitions)       │
│                                                  │
└─���───────────────────────────��───────────────────┘
```

**We own:** What agent knows, how it behaves, what it stores, how it checks own health.

**OpenClaw owns:** How messages arrive, how models called, how sessions persist, how scheduling runs.

Boundary clean: we write markdown and bash scripts; OpenClaw turns that into running agent that talks to people.

Most files under `docs/` = runtime behavior spec, but not all. Pipeline-focused references like `docs/agentic-pipeline-learnings.md` document contributor/CI guardrails, not the OpenClaw runtime contract.