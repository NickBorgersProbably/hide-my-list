---
layout: default
title: Home
---

# hide-my-list Documentation

An AI-powered task manager where users never directly view their task list. The system uses conversational AI to intake tasks, intelligently label them, and surface the right task at the right time.

## Documentation

- [Agent Capabilities](agent-capabilities.md) - Session roles, tool boundaries, and operational ownership across main agent, heartbeat, and isolated cron
- [Heartbeat Checks](heartbeat-checks.md) - Authoritative heartbeat check list (stranded reminders, cron health, drift correction, Notion connectivity, dirty-pull recovery)
- [Architecture](architecture.md) - System architecture, components, and data flow
- [Agentic Pipeline Learnings](agentic-pipeline-learnings.md) - Prescriptive lessons from the agentic review and CI pipeline
- [OpenClaw Integration](openclaw-integration.md) - How the repo maps onto the OpenClaw runtime
- AI Prompts (per-intent):
  - [Shared](ai-prompts/shared.md) - Base prompt, intent dispatch, user preferences context, output handling (entry point)
  - [Intake](ai-prompts/intake.md) - Task intake: inference rules, sub-task generation, reminder detection
  - [Selection](ai-prompts/selection.md) - Task selection: scoring weights, mood mapping
  - [Rejection](ai-prompts/rejection.md) - Shame-safe rejection responses, escalation flow
  - [Cannot Finish](ai-prompts/cannot-finish.md) - Progress gathering, sub-task creation
  - [Check-In](ai-prompts/check-in.md) - Timing, shame-safe templates
  - [Breakdown](ai-prompts/breakdown.md) - Confidence detection, response levels
- [Notion Schema](notion-schema.md) - Database schema and data model
- [Task Lifecycle](task-lifecycle.md) - Task states and transitions
- [User Interactions](user-interactions.md) - User interaction patterns
- [User Preferences](user-preferences.md) - Personalization behavior spec
- [Reward System](reward-system.md) - Multi-channel reward and celebration system

## Design

- [ADHD Priorities](../design/adhd-priorities.md) - Core design principles grounded in ADHD research
