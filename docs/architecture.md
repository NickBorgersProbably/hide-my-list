---
layout: default
title: System Architecture
---

# hide-my-list: System Architecture

## Overview

hide-my-list = AI task manager. Users never see task list. Conversational AI intakes tasks, labels them, surfaces right task based on mood, time, urgency.

Runtime ownership split between main agent, heartbeat, isolated cron: see [Agent Capabilities](agent-capabilities.md).

## High-Level Architecture

```mermaid
flowchart TB
    subgraph Agent["OpenClaw Agent"]
        AI[Conversational AI Layer]
        Scripts[Notion CLI Scripts]
    end

    subgraph Scheduling["OpenClaw Scheduling"]
        HeartbeatCron[Heartbeat Cron<br/>daily]
        DeliverySweepCron[Reminder Delivery Sweep<br/>every 120m]
        JanitorCron[Janitor Cron<br/>weekly]
        ReminderCron[Reminder Cron<br/>every 15m]
        PullMainCron[Pull-Main Cron<br/>every 10m]
    end

    subgraph Messaging["Messaging Surfaces"]
        Web[Web Chat]
        Signal[Signal]
        Telegram[Telegram]
        Discord[Discord]
    end

    subgraph External["External Services"]
        Notion[Notion API<br/>Task Storage]
        OpenAI[OpenAI API<br/>Image Generation]
        GitHub[GitHub Actions<br/>Review Pipeline]
    end

    Messaging <-->|OpenClaw routing| AI
    AI <-->|CRUD operations| Scripts
    Scripts <-->|REST API| Notion
    ReminderCron -->|Isolated cheap-tier: query reminders| Scripts
    PullMainCron -->|Isolated cheap-tier: pull workspace| Scripts
    HeartbeatCron -->|Isolated cheap-tier: daily safety checks + reminder delivery| AI
    DeliverySweepCron -->|Isolated cheap-tier: stranded reminder delivery only| AI
    JanitorCron -->|Isolated Opus: deep weekly audit| AI
```


## How It Works

No standalone server. OpenClaw agent *is* the application. It:

1. **Receives messages** from any configured messaging surface (web chat, Signal, Telegram, Discord, etc.)
2. **Detects intent** from natural language (add task, get task, complete, reject, etc.)
3. **Manages tasks** in Notion database via API
4. **Selects tasks** based on user mood, energy, available time
5. **Breaks down tasks** into concrete, personalized sub-steps
6. **Celebrates completions** with immediate positive reinforcement
7. **Delivers scheduled reminders** even when chat is idle

Interactive conversations: surface-agnostic. Durable cron jobs: isolated cheap-tier sessions for cost efficiency — execute scripts, write handoff files, and run operational checks. Reminder delivery: one-shot `reminder-<page_id>` cron registered at intake fires at exact `remind_at`; `reminder-check` poll + `heartbeat` cron + startup check are safety-net paths only. All cron jobs silent when nothing actionable except the heartbeat cron's explicit `HEARTBEAT_OK` health result.

## Component Architecture

```mermaid
flowchart LR
    subgraph Prompts["Conversation Layer"]
        Intent[Intent Detection]
        Intake[Task Intake]
        Selection[Task Selection]
        Breakdown[Task Breakdown]
        Reward[Reward & Celebration]
    end

    subgraph Scripts["scripts/"]
        NotionCLI[notion-cli.sh<br/>Task CRUD]
        UserTime[user-time-context.sh<br/>User-local time context]
        RewardImg[generate-reward-image.sh<br/>AI Celebration Images]
        RecapVid[generate-weekly-recap.sh<br/>Weekly Recap Video]
        ReminderCheck[check-reminders.sh<br/>Due Reminder Query]
        SecUpdate[security-update.sh<br/>Package Patching]
    end

    subgraph Storage["Notion Database"]
        Tasks[(Tasks)]
    end

    subgraph CI["GitHub Actions"]
        PRTests[PR Tests]
        Review[Multi-Agent Review]
    end

    Intent --> Intake
    Intent --> Selection
    Intake --> NotionCLI
    Selection --> NotionCLI
    NotionCLI --> Tasks
    Breakdown --> NotionCLI
    Reward --> RewardImg
    Intake --> UserTime
    ReminderCheck --> NotionCLI
```


## Request Flow

```mermaid
sequenceDiagram
    participant User
    participant Surface as Messaging Surface
    participant Agent as OpenClaw Agent
    participant Notion as Notion API

    User->>Surface: Types message
    Surface->>Agent: Routed via OpenClaw
    Agent->>Agent: Detect intent

    alt Task Intake
        Agent->>Agent: Extract task details + labels
        Agent->>Notion: Create task (with breakdown)
        Notion-->>Agent: Task ID
        Agent-->>Surface: Confirmation
    else Task Selection
        Agent->>Notion: Query pending tasks
        Notion-->>Agent: Task list (user never sees this)
        Agent->>Agent: Score & select best match
        Agent-->>Surface: "Here's what I'd suggest..."
    else Task Completion
        Agent->>Notion: Update status + timestamp
        Agent-->>Surface: Celebration!
    else Task Rejection
        Agent->>Notion: Update rejection count
        Agent->>Agent: Select alternative
        Agent-->>Surface: "No problem. How about..."
    else Cannot Finish
        Agent->>Agent: Ask what was accomplished
        Agent->>Notion: Create sub-tasks for remainder
        Agent-->>Surface: "Let's break this down..."
    end

    Surface-->>User: Display response
```

## Data Flow

```mermaid
flowchart TD
    subgraph Input["User Input"]
        Msg[Chat Message]
    end

    subgraph Processing["Agent Processing"]
        Intent[Intent Detection]
        Intake[Task Intake]
        Complexity[Complexity Evaluation]
        Breakdown[Personalized Breakdown]
        Select[Task Selection]
        Complete[Completion Handler]
        Reject[Rejection Handler]
        CannotFinish[Cannot Finish Handler]
    end

    subgraph Storage["Notion Database"]
        DB[(Tasks Table)]
    end

    subgraph Output["User Output"]
        Response[Chat Response]
    end

    Msg --> Intent
    Intent -->|"add task"| Intake
    Intent -->|"get task"| Select
    Intent -->|"done"| Complete
    Intent -->|"reject"| Reject
    Intent -->|"cannot finish"| CannotFinish

    Intake --> Complexity
    Complexity -->|Simple| DB
    Complexity -->|Complex| Breakdown
    Breakdown -->|Create parent + sub-tasks| DB

    Select -->|Read pending| DB
    Complete -->|Update status| DB
    Reject -->|Update notes| DB

    CannotFinish --> Breakdown

    Intake --> Response
    Select --> Response
    Complete --> Response
    Reject --> Response
    Breakdown --> Response
```

## Scheduled Reminders

OpenClaw agent: stateless between messages — no persistent process checks clock. For wall-clock reminders ("remind me at 6pm to email Melanie"), system uses **OpenClaw's native one-shot cron** at intake for exact-time delivery, with a recurring polling cron as a safety net:

```mermaid
sequenceDiagram
    participant User
    participant Main as Main Agent (intake turn)
    participant Notion as Notion API
    participant Cron as OpenClaw Cron Store
    participant OneShot as One-Shot Cron Run<br/>(reminder-<page_id>, fires at remind_at)
    participant State as state.json

    User->>Main: "Remind me at 6pm to email Melanie"
    Main->>Notion: create-reminder (Pending, remind_at)
    Notion-->>Main: page_id
    Main->>Cron: CronCreate name=reminder-<page_id> kind=at deleteAfterRun=true
    Main->>User: "Got it — I'll remind you around 6pm"

    Note over Cron,OneShot: Wait until remind_at...

    Cron->>OneShot: Fire one-shot
    OneShot->>Notion: get-page (verify still Pending)
    OneShot->>User: message via Signal channel
    OneShot->>State: Record recent_outbound reminder
    OneShot->>Notion: complete-reminder(sent)
    Note over Cron: Job auto-deletes (deleteAfterRun)

    Note over Cron,User: Safety net runs in parallel
    Note over Cron,User: reminder-check poll catches unfired one-shots
```

**How it works:**

1. At task intake, AI detects reminder language (e.g., "remind me at 6pm PT to call Sarah"), sets `is_reminder = true`, `remind_at` (full ISO 8601 with timezone), `reminder_status = pending`. After `notion-cli.sh create-reminder` returns the Notion `page_id`, the same intake turn calls `CronCreate` to register a one-shot job named `reminder-<page_id>` with `schedule.kind: "at"`, `at: remind_at`, `deleteAfterRun: true`, `sessionTarget: main`. Registering the cron in the same turn also satisfies OpenClaw's `agent-runner-reminder-guard` (which would otherwise append `"Note: I did not schedule a reminder in this turn..."` to the confirmation reply). User-facing success confirmations intentionally say "around <time>" so intake does not overpromise exact wall-clock delivery if the safety-net path ever has to catch a stranded reminder later. See `setup/cron/reminder-delivery.md` for the full contract.
2. At `remind_at`, OpenClaw fires the one-shot cron as a `sessionTarget: main` agent turn running `litellm/claude-haiku-4-5`. The fired turn: reads the Notion row to confirm it is still `Pending`, sends the reminder via the OpenClaw `message` tool (`action: send`, `channel: signal`) with uniform shame-safe wording (`Hey, time to [task]`), atomically updates `state.json.recent_outbound` with a short-lived entry (`type: reminder`, `page_id`, `title`, `status: "sent"`, `sent_at`, `awaiting_response: true`, `expires_at` ~24h later), then runs `scripts/notion-cli.sh complete-reminder PAGE_ID sent` to atomically set `Status → Completed`, `Reminder Status → sent`, `Completed At`. On `ok` outcome the job self-deletes (`deleteAfterRun: true`).
3. **Safety net** — recurring `reminder-check` cron runs every 15 min as an isolated cheap-tier session, executes `scripts/check-reminders.sh`, queries Notion for pending reminders where `remind_at <= now`, and writes the handoff file (default: `.reminder-signal`, overridable via `REMINDER_SIGNAL_FILE` in `.env`) for delivery via AGENTS.md step 6 (opportunistic, on user interaction), `reminder-delivery-sweep` (every 2 hours), or heartbeat Check 1 (daily). This catches anything the one-shot path misses: `CronCreate` failure at intake, gateway down at fire time, or reminders that lack a registered one-shot for any other reason. All delivery paths validate handoff schema first: must be JSON with `reminders` array where each entry has string `page_id`, non-empty string `title`, and string `status`. New writers emit only `sent`; legacy `missed` entries should still be delivered and normalized to `sent`. Wrong shape or status = malformed; delivering session leaves file in place, resolves `OPS_ALERT_SIGNAL_NUMBER` from `.env` to concrete Signal recipient, sends ops alert via the OpenClaw `message` tool (`action: send`, `channel: signal`, `target: "<resolved OPS_ALERT_SIGNAL_NUMBER>"`) describing the malformed handoff, and delivers/completes/deletes nothing. Valid handoff: same delivery sequence as the one-shot (Signal message → `state.json.recent_outbound` write → `complete-reminder sent` → handoff delete).

`state.json.recent_outbound` is the cross-session continuity bridge. It lets a fresh session connect terse follow-ups like "I did it" or "later" to the reminder that was just delivered, even after the reminder page is already completed in Notion. Entries should be pruned after they expire or once the user's reply clearly resolves them. The one-shot cron path and the safety-net path produce identical `recent_outbound` entries, so the next user reply resolves correctly regardless of which path delivered.

**Duplicate-delivery trade-off:** if the one-shot fires and delivers but crashes before `complete-reminder` succeeds, the safety net will pick the still-Pending row up at the next 15-min poll and re-deliver. We accept at-least-once over at-most-once — getting a reminder twice is far better than not getting it at all. The one-shot prompt runs `complete-reminder` immediately after delivery confirmation, with no other work in between, to minimize the duplicate window.

The routine recurring jobs `heartbeat`, `reminder-check`, `reminder-delivery-sweep`, and `pull-main` use `sessionTarget: isolated` with the cheap-tier model (per `setup/model-tiers.json` and the canonical `setup/cron/` specs), `payload.kind: agentTurn`, and `payload.lightContext: true` (skips bootstrap file loading — cron prompts are self-contained scripts/spec readers). The weekly `janitor` cron is also isolated, but uses its explicit Opus model and `payload.lightContext: false` for a full-bootstrap deep audit. Deliberate design: isolated cheap-tier cron with empty bootstrap keeps per-run cost low for routine background work, while multi-step user-facing delivery is decoupled to `litellm/claude-haiku-4-5`, reminder safety-net delivery stays on a short cheap sweep, and deep maintenance is isolated to one weekly high-reasoning sweep. The one-shot delivery cron is registered separately at intake and uses `sessionTarget: main` with `lightContext: false` so it has SOUL.md tone + AGENTS.md state.json conventions in scope. If reminder delivery fails after the one-shot fires: fail visibly without calling `complete-reminder`, and let the safety-net path deliver on its next sweep.

**Timezone handling:** AI converts user times (e.g., "6pm PT", "3pm Central") to full ISO 8601 with timezone offsets at intake. Relative phrases like "tomorrow", "tonight", and day-of-week references must be resolved against the user's configured timezone in `USER.md`, never against the UTC session header. `scripts/user-time-context.sh` provides the user-local date/day context when the agent needs to translate a UTC timestamp before building `remind_at`. Both the one-shot cron's `schedule.at` field and the polling check script compare against UTC — no timezone conversion at fire/check time.

**Cron registration and drift:** AGENTS.md startup and the daily `heartbeat` cron verify each canonical recurring job exists in OpenClaw (`heartbeat`, `reminder-check`, `reminder-delivery-sweep`, `pull-main`, `janitor`), re-creating missing jobs from `setup/cron/` specs. The weekly `janitor` cron owns full drift comparison against the `CronCreate` contract: `name`, `durable`, `schedule`, `prompt`, `sessionTarget`, `model`, absence of direct-delivery `to`, `payload.kind`, `payload.lightContext`, `timeout-seconds`. This guards against fresh-install bootstrap gaps, manual deletion, gateway data loss, or other failure modes that drop or stale a job. `docs/heartbeat-checks.md` = authoritative comparison checklist; `setup/cron/heartbeat.md`, `setup/cron/reminder-delivery-sweep.md`, and `setup/cron/janitor.md` are the cron prompts that read it. One-shot `reminder-<page_id>` jobs are NOT covered by drift / re-registration — they self-delete after firing, so checking their continued presence makes no sense; the safety-net polling path catches anything that fails to fire. See `setup/cron/heartbeat.md`, `setup/cron/reminder-check.md`, `setup/cron/reminder-delivery-sweep.md`, `setup/cron/pull-main.md`, `setup/cron/janitor.md`, and `setup/cron/reminder-delivery.md` for job definitions.

## Technology Choices

| Component | Technology | Rationale |
|-----------|------------|-----------|
| Runtime | OpenClaw Agent | Conversational AI *is* the app — no separate server needed |
| Storage | Notion Database | Zero setup, visual backup, rich API, schema flexibility |
| AI | Claude (via OpenClaw + LiteLLM) | Strong reasoning, structured output, conversation memory |
| Messaging | OpenClaw Surfaces | Interactive chat multi-channel (web, Signal, Telegram, Discord); reminder delivery via one-shot `reminder-<page_id>` cron (exact time); heartbeat cron + startup = safety net |
| CI/CD | GitHub Actions | Multi-agent review pipeline; GitHub-hosted gate jobs handle untrusted dispatch, self-hosted Codex reviewers inherit homelab proxy and VLAN restrictions |
| Scripts | Bash + curl | Minimal dependencies, runs anywhere |
| Scheduled Reminders | OpenClaw native one-shot cron (`schedule.kind: at`, `deleteAfterRun: true`) registered at intake, with recurring `reminder-check` poll + `.reminder-signal` handoff as safety net | One-shot fires at exact `remind_at`; safety-net poll catches `CronCreate` failures or unfired jobs |
| Workspace Sync | OpenClaw durable cron + pull-main.sh | Isolated cron every 10 min keeps workspace current, recovers dirty pulls |
| Image Generation | OpenAI gpt-image-1 | Unique AI images for reward novelty |
| Video | ffmpeg | Weekly recap compilation |

## Core Runtime Variables

| Variable | Purpose |
|----------|---------|
| `NOTION_API_KEY` | Notion integration token |
| `NOTION_DATABASE_ID` | Tasks database identifier |
| `OPENAI_API_KEY` | OpenAI API key for reward image generation |
| `GITHUB_PAT` | Optional PAT for GitHub-maintenance scripts when `gh` not already authenticated |
| `REMINDER_SIGNAL_FILE` | Repo-root reminder handoff filename (default: `.reminder-signal`) |

## Prerequisites

| Dependency | Purpose |
|------------|---------|
| `python3` | JSON payload construction, image decoding |
| `curl` | API calls (Notion, OpenAI) |
| `ffmpeg` | Weekly recap video generation |
| `bc` | Arithmetic in recap script |

## Security Architecture

```mermaid
flowchart TB
    subgraph Sandbox["OpenClaw Sandbox"]
        Agent[Agent]
        Scripts[Scripts]
        Cron[Durable Cron Jobs]
    end

    subgraph Proxy["Squid Proxy"]
        ACL[Domain Allowlist]
    end

    subgraph External["External"]
        Notion[api.notion.com]
        OpenAI[api.openai.com]
        GitHub[api.github.com]
        Research[PubMed, CHADD, etc.]
    end

    subgraph CIEnv["GitHub Actions"]
        Gate[Security Gate Jobs<br/>ubuntu-latest]
        CI[Codex Reviewers<br/>self-hosted]
        FullNet[GitHub-hosted Egress]
    end

    Agent --> Proxy
    Proxy -->|Allowed| Notion
    Proxy -->|Allowed| OpenAI
    Proxy -->|Allowed| GitHub
    Proxy -->|Allowed| Research
    Proxy -.->|Blocked| FullNet

    CI --> Proxy
    Gate --> FullNet

    Cron -->|Scheduled checks| Scripts
```

- **Network isolation**: Agent behind squid proxy with domain allowlist; kernel-level egress rules enforce independently of container
- **CI separation**: GitHub Actions reviewers have no access to infrastructure or home systems
- **Credential handling**: API keys and optional `GITHUB_PAT` in `.env` (gitignored), never logged or committed, runtime scripts load only needed variables per shell
- **Least privilege**: PR test workflows read-only permissions
- **No required webhook listener**: Durable cron handles core ops; optional GitHub-triggered webhook paths are extra inbound surface only when configured

Full security architecture — agent trust model, threat model, prompt injection analysis — see [SECURITY.md](../SECURITY.md).
