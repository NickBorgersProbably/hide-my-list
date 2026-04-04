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
    end

    subgraph Scheduling["OpenClaw Scheduling"]
        Heartbeat[Heartbeat<br/>every 30m]
        ReminderCron[Reminder Cron<br/>every 5m]
        PipelineCron[Pipeline Cron<br/>every 2m]
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
    ReminderCron -->|Checks due reminders| Scripts
    PipelineCron -->|Checks GitHub status| Scripts
    Heartbeat -->|Health checks| AI
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
        ReminderCheck[check-reminders.sh<br/>Due Reminder Query]
        GHStatus[check-github-status.sh<br/>GitHub Status]
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
    ReminderCheck --> NotionCLI
    GHStatus --> Review
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

The OpenClaw agent model is stateless between messages — there is no persistent process to check a clock. To support wall-clock reminders ("remind me at 6pm to email Melanie"), the system uses **OpenClaw's durable cron** to periodically run a reminder check:

```mermaid
sequenceDiagram
    participant Cron as OpenClaw Cron<br/>(every 5 min)
    participant Script as check-reminders.sh
    participant Notion as Notion API
    participant Agent as OpenClaw Agent
    participant User

    Cron->>Agent: Trigger reminder-check job
    Agent->>Script: Run check-reminders.sh
    Script->>Notion: Query reminders where remind_at <= now
    Notion-->>Script: Due reminder tasks
    Script-->>Agent: Signal file with due reminders
    Agent->>User: Deliver reminder message
    Agent->>Notion: Mark reminder as sent/completed
```

**How it works:**

1. During task intake, the AI detects reminder-style language (e.g., "remind me at 6pm PT to call Sarah") and sets `is_reminder = true`, `remind_at` (full ISO 8601 with timezone), and `reminder_status = pending`.
2. A durable cron job (`reminder-check`) runs every 5 minutes via OpenClaw's native scheduling.
3. The cron job triggers the agent to run `scripts/check-reminders.sh`, which queries Notion for pending reminders where `remind_at <= now`.
4. If due reminders are found, `check-reminders.sh` writes `.reminder-signal`; the same cron-driven agent session reads that file, delivers the reminders, marks them as `sent` or `missed` in Notion, and deletes the handoff file.
5. Reminders more than 15 minutes past due are flagged as `missed` but still delivered with a note.
6. The cron job only fires when the agent is idle — it won't interrupt the user mid-task, which is better for ADHD focus.

**Timezone handling:** The AI converts user-specified times (e.g., "6pm PT", "3pm Central") to full ISO 8601 timestamps with timezone offsets at intake time. The check script compares against UTC — no timezone conversion at check time.

**Cron job expiry:** Durable cron jobs auto-expire after 7 days. The heartbeat (every 30 min) verifies the cron job is registered and re-creates it if missing. See `setup/cron/reminder-check.md` for the job definition.

## Technology Choices

| Component | Technology | Rationale |
|-----------|------------|-----------|
| Runtime | OpenClaw Agent | Conversational AI *is* the app — no separate server needed |
| Storage | Notion Database | Zero setup, visual backup, rich API, schema flexibility |
| AI | Claude (via OpenClaw + LiteLLM) | Strong reasoning, structured output, conversation memory |
| Messaging | OpenClaw Surfaces | Multi-channel by default (web, Signal, Telegram, Discord) |
| CI/CD | GitHub Actions | Multi-agent review pipeline; GitHub-hosted gate jobs handle untrusted dispatch, while self-hosted Codex reviewers inherit the homelab proxy and VLAN restrictions |
| Scripts | Bash + curl | Minimal dependencies, runs anywhere |
| Scheduled Reminders | OpenClaw durable cron + check-reminders.sh | Native cron every 5 min, heartbeat re-registers on expiry |
| Pipeline Monitoring | OpenClaw durable cron + check-github-status.sh, plus optional `AGENT_WEBHOOK_URL` callback | Cron polls GitHub every 2 min; an optional external wake-up endpoint configured through GitHub Actions repo variable `vars.AGENT_WEBHOOK_URL` provides faster review-complete notification when enabled |
| Image Generation | OpenAI gpt-image-1 | Unique AI images for reward novelty |
| Video | ffmpeg | Weekly recap compilation |

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `NOTION_API_KEY` | Notion integration token |
| `NOTION_DATABASE_ID` | Tasks database identifier |
| `OPENAI_API_KEY` | OpenAI API key for reward image generation |
| `GITHUB_PAT` | GitHub personal access token (optional, for higher rate limits) |
| `REMINDER_SIGNAL_FILE` | Path for reminder signal handoff (default: `.reminder-signal`) |

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

- **Network isolation**: Agent runs behind squid proxy with domain allowlist; kernel-level egress rules enforce this independently of the container
- **CI separation**: GitHub Actions reviewers have no access to infrastructure or home systems
- **Credential handling**: API keys in `.env` (gitignored), never logged or committed
- **Least privilege**: PR test workflows have read-only permissions
- **Minimal ingress**: Cron handles routine pipeline polling; the optional `AGENT_WEBHOOK_URL` callback is a narrow [A]-only signal path (untrusted input without [B] credentials or [C] external actions) rather than a general control surface

For the full security architecture — including agent trust model, threat model, and prompt injection analysis — see [SECURITY.md](../SECURITY.md).
