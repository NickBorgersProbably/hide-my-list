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

## Envelope Timezone

OpenClaw also reads `agents.defaults.envelopeTimezone` from `openclaw.json`.
For hide-my-list, set that field during first setup to the same IANA timezone
identifier stored in `USER.md` (for example, `America/Chicago`).

Why it matters: OpenClaw injects a `Current time:` line into each prompt. If
`envelopeTimezone` is unset, that line stays UTC-only, and the agent can reason
about relative dates like "tomorrow" against the wrong calendar day for users
outside UTC.

Canonical setup path:
- Put the user's timezone in `USER.md`
- Copy that same TZ identifier into `setup/openclaw.json.template` when creating
  `~/.openclaw/openclaw.json`

## Heartbeat

OpenClaw heartbeat = built-in periodic trigger configured in `openclaw.json`:

```json
"heartbeat": {
  "every": "60m",
  "model": "litellm/<cheap-tier model>",
  "lightContext": true,
  "isolatedSession": true
}
```

Every 60 min, OpenClaw creates short agent session, reads `HEARTBEAT.md`, executes checks. Uses the cheap-tier model from `setup/openclaw.json.template` (`agents.defaults.heartbeat.model`, which must match `modelTiers.cheap`) for routine operational tasks — heartbeat checks are scripted health checks that don't need reasoning.

`lightContext: true` filters bootstrap to only `HEARTBEAT.md` (no AGENTS.md, SOUL.md, IDENTITY.md, TOOLS.md, USER.md, MEMORY.md). Heartbeat reads `docs/heartbeat-checks.md` on demand through its file tools, so the full spec is still available — it just doesn't sit in bootstrap for every run. `isolatedSession: true` skips replaying prior conversation transcripts into the heartbeat's context. Together they cut heartbeat per-run token cost substantially without changing what checks the heartbeat performs. Reminder delivery not via `heartbeat.target`; Check 1 sends reminders explicitly with OpenClaw `message` tool (`action: send`, `channel: signal`). `target` field only controls where generic non-`HEARTBEAT_OK` output routes; without it, defaults to `"none"`, silently discarded ([openclaw/openclaw#29215](https://github.com/openclaw/openclaw/issues/29215)).

**Our usage:** Two roles:
1. **Reminder-delivery backstop:** Isolated `reminder-check` cron only writes `.reminder-signal` — no user delivery. Heartbeat Check 1 reads stranded signal files, validates, delivers to Signal via `message` tool every 60 min. After each successful send, atomically updates `state.json.recent_outbound` (read-merge-prune-write via temp file + rename, preserving all other state fields) before running `complete-reminder`. If the state write fails, delivery halts — no `complete-reminder`, no handoff delete, ops alert surfaces instead. After all reminders in the batch are processed, deletes the handoff file once. AGENTS.md startup check provides the same delivery sequence for faster opportunistic delivery when user is active.
2. **Cron safety net:** Verify durable cron jobs still match canonical `CronCreate` specs in `setup/cron/`: expired jobs get re-registered; drifted jobs get patched back. Comparison covers full effective registration contract: `name`, `durable`, `schedule`, `prompt`, `sessionTarget`, `model`, absence of direct-delivery `to`, `payload.kind`, `payload.lightContext`, `timeout-seconds`. `docs/heartbeat-checks.md` = authoritative comparison checklist (HEARTBEAT.md is a bootstrap stub that delegates to it).

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

**Our usage:** Two recurring durable cron jobs replace former bash daemons/manual sync steps, plus a per-reminder one-shot family registered at intake:

| Job | Schedule | Replaces / purpose |
|-----|----------|----------|
| `reminder-check` | `*/15 * * * *` | `reminder-daemon.sh` (bash while-loop); now safety-net only — primary delivery is the one-shot below |
| `pull-main` | `*/10 * * * *` | Manual `git pull origin main` hygiene; cron drift correction now through heartbeat |
| `reminder-<page_id>` | `kind: "at"` (one-shot, registered at intake) | User-facing reminder delivery at exact `remind_at`; self-deletes after firing |

**Why better than daemons:**
- No PID files, no silent death, no orphaned processes
- OpenClaw manages scheduling; failures visible in session
- One-shot `reminder-<page_id>` cron delivers at exact `remind_at` (see `setup/cron/reminder-delivery.md`); safety-net `scripts/check-reminders.sh` writes `.reminder-signal` handoff for AGENTS.md step 5 + heartbeat Check 1 to deliver if the one-shot misses
- Cron fires only when REPL idle — better for ADHD, won't interrupt mid-task

**Robustness backstop:** Heartbeat (every 60 min) re-creates any canonical recurring cron job that has gone missing and patches drift via comparison against `CronCreate` blocks. Covers manual deletion, gateway data loss, or other failure modes that drop the job. One-shot `reminder-<page_id>` jobs are out of scope for this check — they self-delete after firing.

**Current registration contract:** the two recurring jobs (`reminder-check`, `pull-main`) run as isolated cheap-tier sessions with `sessionTarget: isolated`, the concrete `model:` value from the canonical `CronCreate` blocks in `setup/cron/` (those lines must match `modelTiers.cheap` in `setup/openclaw.json.template`), `payload.kind: agentTurn`, `payload.lightContext: true` (OpenClaw strips bootstrap to empty for `lightweight` cron runs — our cron prompts are self-contained scripts, so no bootstrap context is needed), `timeout-seconds: 300`. Deliberate: separates cheap polling work from user-facing delivery. Previous architecture used `sessionTarget: main` — loaded full Opus context (~200k tokens) for routine script work. Isolated cheap-tier cron cuts per-run cost by orders of magnitude. The per-reminder one-shot (`reminder-<page_id>`) uses a different profile — `sessionTarget: main` with `lightContext: false` so the fired turn has SOUL.md tone + AGENTS.md state.json conventions in scope, while still running on the cheap-tier model; full contract in `setup/cron/reminder-delivery.md`. Recurring cron prompts end with `NO_REPLY` — never produce user-facing output. The one-shot prompt does deliver to the user (via the `message` tool inside the agent turn).

Isolated cron sessions intentionally narrow. Script runners, not substitute for main agent or heartbeat control paths. Detailed ownership split in [Agent Capabilities](agent-capabilities.md).

### Production Timing Recommendation

For production, use these timings unless clear reason to pay for tighter polling:

| Mechanism | Recommended cadence | Why |
|-----------|---------------------|-----|
| `reminder-<page_id>` (one-shot) | Fires at exact `remind_at` (registered at intake) | Primary user-facing reminder delivery; self-deletes on success |
| Heartbeat | Every 60 minutes | Reminder safety-net delivery, recurring-cron drift correction, Notion/env health |
| `reminder-check` | Every 15 minutes | Safety-net polling; writes `.reminder-signal` for heartbeat/startup delivery when one-shot fails to fire |
| `pull-main` | Every 10 minutes | Cheap script-only sync path; keeps workspace fresh |

Core principle: the one-shot cron registered at intake is the primary delivery path — fires at exact `remind_at`. The recurring `reminder-check` poll + handoff + heartbeat path is the safety net for `CronCreate` failures, gateway data loss, or jobs that fail to fire; in that fallback case, fully idle worst-case latency is ~75 min (15-min poll + 60-min heartbeat) unless the user interacts sooner.

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

**Our role:** Zero for transport mechanics. We write conversational responses; OpenClaw delivers them. Interactive conversations use normal main-agent routing. Recurring cron jobs (`reminder-check`, `pull-main`) = isolated cheap-tier sessions (query-only, no user delivery). Per-reminder one-shot crons (`reminder-<page_id>`) = `sessionTarget: main` cheap-tier sessions that DO deliver to the user via the `message` tool. Safety-net path: `reminder-check` writes `.reminder-signal` handoff, AGENTS.md step 5 + heartbeat Check 1 deliver from there.

## Model Routing (LiteLLM Proxy)

OpenClaw supports multiple model providers. We route through LiteLLM proxy on Tailscale network:

```json
"models": {
  "providers": {
    "litellm": {
      "baseUrl": "https://llm.featherback-mermaid.ts.net/v1",
      "models": [
        { "id": "<expensive-tier model>", ... },
        { "id": "<medium-tier model>", ... },
        { "id": "<cheap-tier model>", ... }
      ]
    }
  }
}
```

Canonical model list and tier mappings live in `setup/openclaw.json.template` (see `modelTiers`). `scripts/validate-model-refs.sh` enforces that every `litellm/<id>` reference in classifier-listed spec files resolves against that list, that tier mappings are consistent with agent config (including `agents.defaults.heartbeat.model` matching `modelTiers.cheap`), and that cron specs plus sibling docs stay aligned with the cheap tier contract.

- **Primary model (expensive tier):** Whatever `modelTiers.expensive` maps to for conversations and task management
- **Heartbeat model (cheap tier):** Whatever `modelTiers.cheap` maps to for routine health checks
- **Cron model (cheap tier):** Whatever `modelTiers.cheap` maps to for both isolated recurring cron work (reminder polling, workspace sync) and the per-reminder one-shot delivery cron
- **Codex CLI model:** GPT-5.4, configured separately in `.codex/config.toml` via `.devcontainer/configure-codex.sh`; not served through the OpenClaw models array above.

**Our role:** No direct interaction with model selection. Prompts in `docs/ai-prompts/` model-agnostic. OpenClaw picks model from config.

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

**Reminder confirmation guard:** OpenClaw's `agent-runner-reminder-guard` (in the OpenClaw plugin SDK, post-process step in the agent runner) appends `"Note: I did not schedule a reminder in this turn, so this will not trigger automatically."` to any model reply that matches a reminder-commitment regex unless the same turn registered a cron job (`successfulCronAdds > 0`) or an enabled cron shares the current `sessionKey`. The fix lives in the intake flow, not in a hook: `docs/ai-prompts/intake.md` REMINDER PERSISTENCE step requires the agent to call `CronCreate` in the same turn as `notion-cli.sh create-reminder`, which suppresses the guard note. No `PostToolUse` hook is needed — and any prompt-level instruction telling the model not to produce the note would be ineffective, since the note is appended by the framework after the model reply.

**Key distinction:**
- **OpenClaw hooks** (`openclaw hooks list`): Platform events — bootstrap, session-memory, command-logging. Configured in `openclaw.json`.
- **Claude Code hooks** (`.claude/settings.json`): Tool-use events — PostToolUse, PreToolUse. Configured per-project. We currently use only the `SessionStart` hook for caveman-mode prompt prefix.

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
