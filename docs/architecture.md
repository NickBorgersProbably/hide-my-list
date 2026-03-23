---
layout: default
title: System Architecture
---

# hide-my-list: System Architecture

## Overview

hide-my-list is an AI-powered task manager where users never directly view their task list. The system uses conversational AI to intake tasks, intelligently label them, and surface the right task at the right time based on user mood, available time, and task urgency.

## High-Level Architecture

```mermaid
flowchart TB
    subgraph Agent["OpenClaw Agent"]
        AI[Conversational AI Layer]
        Scripts[Notion CLI Scripts]
        Webhook[Webhook Signal Receiver]
        Reminder[Reminder Daemon]
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
    Reminder -->|Polls reminders| Notion
    GitHub -->|PR review complete| Webhook
    Webhook -->|Signal file| AI
    Reminder -->|Signal file| AI
```


## How It Works

There is no standalone server. The OpenClaw agent *is* the application. It:

1. **Receives messages** from any configured messaging surface (web chat, Signal, Telegram, Discord, etc.)
2. **Detects intent** from natural language (add task, get task, complete, reject, etc.)
3. **Manages tasks** in a Notion database via API
4. **Selects tasks** based on user mood, energy, and available time
5. **Breaks down tasks** into concrete, personalized sub-steps
6. **Celebrates completions** with immediate positive reinforcement
7. **Delivers scheduled reminders** even when the chat is idle

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
        RewardImg[generate-reward-image.sh<br/>AI Celebration Images]
        RecapVid[generate-weekly-recap.sh<br/>Weekly Recap Video]
        ReminderDaemon[reminder-daemon.sh<br/>Reminder Loop]
        ReminderCheck[check-reminders.sh<br/>Due Reminder Query]
        WebhookSig[webhook-signal.sh<br/>CI Notifications]
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
    ReminderDaemon --> ReminderCheck
    ReminderCheck --> NotionCLI
    Review --> WebhookSig
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

The OpenClaw agent model is stateless between messages — there is no persistent process to check a clock. To support wall-clock reminders ("remind me at 6pm to email Melanie"), the system uses a **signal-file pattern** identical to the webhook receiver:

```mermaid
sequenceDiagram
    participant Daemon as reminder-daemon.sh
    participant Script as check-reminders.sh
    participant Notion as Notion API
    participant Signal as .reminder-signal
    participant Agent as OpenClaw Agent
    participant User

    Daemon->>Script: Runs every 5 minutes
    Script->>Notion: Query reminders where remind_at <= now
    Notion-->>Script: Due reminder tasks
    Script->>Signal: Rewrite signal file with current due reminders
    Agent->>Signal: Periodic check (same as webhook)
    Agent->>User: Deliver reminder message
    Agent->>Notion: Mark reminder as sent/completed
```

**How it works:**

1. During task intake, the AI detects reminder-style language (e.g., "remind me at 6pm PT to call Sarah") and sets `is_reminder = true`, `remind_at` (full ISO 8601 with timezone), and `reminder_status = pending`.
2. The local `reminder-daemon.sh` loop runs `check-reminders.sh` every 5 minutes (configurable).
3. The script queries Notion for pending reminders where `remind_at <= now`.
4. For each successful Notion query, it rewrites `.reminder-signal` with the current due reminders. Reminders remain in the due query until the agent confirms delivery in Notion, so the signal file does not need to preserve stale local entries across cycles.
5. The agent picks up the signal file (same polling mechanism as the webhook signal), delivers the reminder to the user, and then marks the reminder as `sent` or `missed` in Notion, updating the task's main `Status` when appropriate. This gives at-least-once delivery without leaving already-delivered reminders queued forever.
6. Reminders more than 15 minutes past due are flagged as `missed` but still delivered with a note.

**Timezone handling:** The AI converts user-specified times (e.g., "6pm PT", "3pm Central") to full ISO 8601 timestamps with timezone offsets at intake time. The reminder daemon compares against UTC — no timezone conversion at check time.

### Operations

**Starting the daemon:**

```bash
scripts/reminder-daemon.sh              # loop forever, poll every 5 min
scripts/reminder-daemon.sh --once       # single check and exit
scripts/reminder-daemon.sh --interval 120  # custom interval (seconds)
```

**Environment overrides:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `REMINDER_POLL_INTERVAL` | `300` (5 min) | Polling interval in seconds |
| `REMINDER_LOG_FILE` | `/tmp/reminder-daemon.log` | Log output location |
| `REMINDER_PID_FILE` | `/tmp/reminder-daemon.pid` | PID file to prevent duplicate daemons |
| `REMINDER_HEARTBEAT_FILE` | `/tmp/reminder-daemon.heartbeat` | Timestamp updated each cycle for staleness detection |
| `REMINDER_STALE_SECONDS` | `900` (15 min) | Max heartbeat age before health check treats daemon as dead |

**Health check:**

```bash
scripts/reminder-health-check.sh              # check status, exit 0 if healthy
scripts/reminder-health-check.sh --restart     # restart automatically if unhealthy
```

The health check verifies three conditions:
1. PID file exists and the referenced process is running
2. Log file exists
3. Heartbeat file was written within the staleness threshold (default: 15 min)

If any check fails, the script exits non-zero with diagnostic output. With `--restart`, it cleans up stale files and restarts the daemon.

**Lifecycle notes:**

- The PID file prevents multiple daemon instances — if one is already running, a second invocation exits with an error.
- The PID file is automatically cleaned up on exit (via `trap`). If a daemon crashes without cleanup, a stale PID file is detected and removed on the next start.
- The daemon traps SIGTERM, SIGINT, and SIGHUP for clean shutdown logging.
- A heartbeat file is updated after each reminder check cycle. If the heartbeat goes stale, the health check detects the daemon as dead even if the process is hung.
- Logs are appended to `REMINDER_LOG_FILE` — check this file to debug missed or delayed reminders.
- Use `--once` for testing or one-shot cron setups.

## Technology Choices

| Component | Technology | Rationale |
|-----------|------------|-----------|
| Runtime | OpenClaw Agent | Conversational AI *is* the app — no separate server needed |
| Storage | Notion Database | Zero setup, visual backup, rich API, schema flexibility |
| AI | Claude (via OpenClaw) | Strong reasoning, structured output, conversation memory |
| Messaging | OpenClaw Surfaces | Multi-channel by default (web, Signal, Telegram, Discord) |
| CI/CD | GitHub Actions | Multi-agent review pipeline with full internet for research |
| Scripts | Bash + curl | Minimal dependencies, runs anywhere |
| Scheduled Reminders | reminder-daemon.sh + check-reminders.sh | Local polling every 5 min without GitHub cron |
| Image Generation | OpenAI gpt-image-1 | Unique AI images for reward novelty |
| Video | ffmpeg | Weekly recap compilation |

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `NOTION_API_KEY` | Notion integration token |
| `NOTION_DATABASE_ID` | Tasks database identifier |
| `OPENAI_API_KEY` | OpenAI API key for reward image generation |
| `WEBHOOK_PORT` | CI notification webhook port (default: 9199) |
| `REMINDER_SIGNAL_FILE` | Path for reminder signal handoff (default: `.reminder-signal`) |
| `REMINDER_POLL_INTERVAL` | Reminder daemon polling interval in seconds (default: 300) |

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
        Webhook[Webhook Listener]
        Reminder[Reminder Daemon]
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
        CI[Claude Code Reviewers]
        FullNet[Full Internet Access]
    end

    Agent --> Proxy
    Proxy -->|Allowed| Notion
    Proxy -->|Allowed| OpenAI
    Proxy -->|Allowed| GitHub
    Proxy -->|Allowed| Research
    Proxy -.->|Blocked| FullNet

    CI --> FullNet

    Webhook -->|Only writes timestamp| Agent
```

- **Network isolation**: Agent runs behind squid proxy with domain allowlist
- **Webhook security**: Listener discards all request data, only writes self-generated timestamp
- **CI separation**: GitHub Actions reviewers have full internet but no access to infrastructure or home systems
- **Credential handling**: API keys in `.env` (gitignored), never logged or committed
- **Least privilege**: PR test workflows have read-only permissions

For the full security architecture — including agent trust model, threat model, and prompt injection analysis — see [SECURITY.md](../SECURITY.md).
