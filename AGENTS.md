# AGENTS.md — hide-my-list

## What this project is

An ADHD-informed task manager where users never see their task list. The AI handles intake, labeling, selection, breakdown, and celebration. This is an **OpenClaw agent** — the conversational AI layer *is* the application.

## Architecture

- **Runtime**: OpenClaw agent (no standalone server)
- **Storage**: Notion database via API
- **Scripts**: `scripts/` directory contains Notion CLI helpers and infrastructure tooling
- **Docs**: `docs/` contains architecture, prompt design, and research documentation
- **Design**: `design/` contains ADHD-informed design priorities and principles

## Key files

- `docs/ai-prompts.md` — The prompt architecture. This is the core of the application.
- `docs/architecture.md` — System design and data flow
- `docs/task-lifecycle.md` — Task states: Pending → In Progress → Completed (with rejection/breakdown flows)
- `docs/notion-schema.md` — Notion database schema
- `docs/user-interactions.md` — Conversation patterns and intent detection
- `docs/user-preferences.md` — Personalization system
- `docs/reward-system.md` — Multi-channel reward mechanics
- `design/adhd-priorities.md` — Core design principles grounded in ADHD research
- `scripts/notion-cli.sh` — Notion API helper for task CRUD operations
- `scripts/webhook-signal.sh` — Minimal webhook receiver for CI/CD notifications

## Conversation personality

From `docs/ai-prompts.md`:
- Casual and brief — like texting a helpful friend
- Confident in suggestions, collaborative on rejections
- No emojis unless user uses them first
- No formal greetings, use contractions naturally
- Keep responses under 50 words unless explaining something
- **Never show the user their full task list**
- Ask at most ONE question at a time

## Review pipeline

PRs are reviewed by a multi-agent Claude Code pipeline:
1. Design Review — validates intent fulfillment and design quality
2. Code Review — code quality, error handling, safety
3. Test Review — test coverage and quality
4. Concurrency Review — thread safety and async correctness
5. Documentation Review — keeps docs in sync with changes
6. Psych Research Review — validates against ADHD clinical research
7. Merge Decision — synthesizes all reviews into GO/NO-GO

## When making changes

- Update relevant docs when changing behavior
- The psych reviewer will validate user-facing changes against ADHD research
- Infrastructure/CI changes skip the psych review automatically
- All changes go through PR with the full review pipeline
