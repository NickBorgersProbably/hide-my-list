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

This repository is designed to be deployed directly as an OpenClaw workspace (`~/.openclaw/workspace/`). The markdown files at the root (`SOUL.md`, `AGENTS.md`, `HEARTBEAT.md`, etc.) are the bootstrap files that OpenClaw can load at session start — they define the agent's personality and operations. Production health checks run through the durable cron job in `setup/cron/heartbeat.md`.

- **Agent**: OpenClaw-managed conversational AI (Claude via LiteLLM proxy)
- **Storage**: Notion database via API
- **Scheduling**: OpenClaw durable cron jobs (reminders, workspace sync)
- **Messaging**: Signal (via OpenClaw channel routing)
- **Review Pipeline**: GitHub Actions with multi-agent Codex review

See [docs/openclaw-integration.md](docs/openclaw-integration.md) for how the system maps to OpenClaw's architecture.

## Quick start

```bash
# Clone as OpenClaw workspace
git clone https://github.com/NickBorgersProbably/hide-my-list.git ~/.openclaw/workspace

# Install the repo-managed git hooks for this worktree
cd ~/.openclaw/workspace && bash .githooks/install-hooks.sh

# Create .env from .env.template and fill in the values you need
cp ~/.openclaw/workspace/.env.template ~/.openclaw/workspace/.env

# Run the bootstrap script
cd ~/.openclaw/workspace && bash setup/bootstrap.sh
```

When you create `~/.openclaw/openclaw.json`, set
`agents.defaults.envelopeTimezone` to the same IANA timezone identifier used in
`USER.md` (for example, `America/Chicago`). This keeps OpenClaw's injected
`Current time:` line in the user's local time. If it is unset, reminder
correctness still comes from `USER.md` plus `scripts/user-time-context.sh` when
the visible session timestamp is UTC, but prompt context is less direct.

`setup/bootstrap.sh` also provisions OpenClaw's media staging directories under
`~/.openclaw/media/outbound` with traversable permissions so Signal can read
staged attachments such as reward images.

See [setup/README.md](setup/README.md) for full setup instructions.

## Git hooks

Install the repo hooks in every worktree you plan to commit from:

```bash
bash .githooks/install-hooks.sh
```

`core.hooksPath` is stored per worktree, so re-run that after each `git worktree add`.
`pre-commit` handles the fast staged-file checks, and `pre-push` reruns the
deterministic CI-equivalent checks for changed scripts, docs, and
workflow-related paths, so those failures are caught locally before GitHub is
the first place they fail.

## Research-informed design

Every feature is evaluated against ADHD clinical research:
- Executive function support (Barkley model)
- Emotional regulation (Hallowell-Ratey framework)
- Time perception and time blindness research
- Motivation and reward systems (variable ratio reinforcement)
- Cognitive load management

The CI pipeline includes a **psychological research evidence reviewer** that validates changes against these frameworks.

## Documentation

- [OpenClaw Integration](docs/openclaw-integration.md)
- [Architecture](docs/architecture.md)
- [AI Prompts](docs/ai-prompts/shared.md) (entry point; per-intent prompts live in `docs/ai-prompts/`)
- [Task Lifecycle](docs/task-lifecycle.md)
- [Notion Schema](docs/notion-schema.md)
- [User Interactions](docs/user-interactions.md)
- [User Preferences](docs/user-preferences.md)
- [Reward System](docs/reward-system.md)
- [ADHD Design Priorities](design/adhd-priorities.md)
- [Security Architecture](SECURITY.md)
- [Setup & Operations](setup/README.md)

## License

MIT — see [LICENSE](LICENSE)
