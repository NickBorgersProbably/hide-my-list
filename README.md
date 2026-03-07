# hide-my-list

An ADHD-informed task manager where you never see your task list.

## What is this?

hide-my-list is a conversational AI agent that manages your tasks for you. You tell it what you need to do, and it handles the rest — breaking tasks down, tracking them, and surfacing the right one when you're ready to work. You never look at a list. You never feel overwhelmed by a wall of undone items.

The core insight: **for people with ADHD, seeing a long task list isn't motivating — it's paralyzing.** Traditional task managers make this worse. hide-my-list takes a different approach.

## How it works

- **Talk to it** — describe what you need to do in natural language
- **It labels and stores** — work type, urgency, time estimate, energy required (in Notion)
- **It picks for you** — when you have time, it selects a task based on your current energy and mood
- **It breaks things down** — vague tasks get personalized, concrete sub-steps
- **It celebrates wins** — completion triggers immediate positive reinforcement
- **You never see the list** — that's the whole point

## Architecture

This is an **OpenClaw agent** backed by a **Notion database**. There is no standalone server — the AI conversation layer *is* the application.

- **Agent**: OpenClaw-managed conversational AI
- **Storage**: Notion database via API
- **Review Pipeline**: GitHub Actions with multi-agent Claude Code review (design, code, test, concurrency, docs, psych research)

See [docs/](docs/) for detailed architecture and design documentation.

## Research-informed design

Every feature is evaluated against ADHD clinical research:
- Executive function support (Barkley model)
- Emotional regulation (Hallowell-Ratey framework)
- Time perception and time blindness research
- Motivation and reward systems (variable ratio reinforcement)
- Cognitive load management

The CI pipeline includes a **psychological research evidence reviewer** that validates changes against these frameworks.

## Documentation

- [Architecture](docs/architecture.md)
- [AI Prompts](docs/ai-prompts.md)
- [Task Lifecycle](docs/task-lifecycle.md)
- [Notion Schema](docs/notion-schema.md)
- [User Interactions](docs/user-interactions.md)
- [User Preferences](docs/user-preferences.md)
- [Reward System](docs/reward-system.md)

## License

MIT — see [LICENSE](LICENSE)
