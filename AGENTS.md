# AGENTS.md — hide-my-list

ADHD-informed task manager in Rust. Users never see their task list; conversational AI handles intake, labeling, and surfacing the right task based on mood, energy, and urgency.

**Status:** Hello-world alpha. Health endpoint and conversation echo stub are live. Domain types and in-memory storage are in place. Notion and Anthropic integrations are next.

## Build & test commands

```bash
cargo build                        # Debug build
cargo build --release              # Release build
cargo fmt --all -- --check         # Check formatting (pre-commit hook runs this)
cargo clippy --all-targets -- -D warnings  # Lint (pre-push hook runs this)
cargo test --lib --bins            # Unit tests only
cargo test --test '*'              # Integration tests only
cargo test                         # All tests
```

Docker:
```bash
docker compose build               # Build image
docker compose up                  # Run locally on :8080
curl -sf http://localhost:8080/health  # Smoke test
```

## Git hooks

Hooks live in `.githooks/` and are installed via `git config core.hooksPath .githooks` (the devcontainer does this automatically).

- **pre-commit:** `cargo fmt --check` — formatting only, fast
- **pre-push:** `cargo clippy -- -D warnings` + `cargo test` — full lint and test suite

Never bypass hooks with `--no-verify`. If formatting fails, run `cargo fmt` and commit again.

## Project layout

```
src/
  main.rs          # Entry point: config, server, graceful shutdown
  lib.rs           # Module re-exports
  config.rs        # Typed Config from env vars (PORT, RUST_LOG, ENVIRONMENT)
  error.rs         # AppError enum → HTTP status codes
  api/
    mod.rs         # Router, AppState (config + task_repo + uptime)
    health.rs      # GET /health
    conversation.rs # POST /api/v1/conversation (echo stub)
  domain/
    task.rs        # Task struct, TaskStatus, WorkType, EnergyLevel, RewardTriggerType
    preference.rs  # UserPreference, Mood
  storage/
    mod.rs         # TaskRepository trait
    memory.rs      # InMemoryTaskRepository (RwLock<HashMap>)
tests/
  health_test.rs        # Health endpoint integration test
  conversation_test.rs  # Conversation endpoint integration tests
docs/              # Jekyll site (architecture diagrams, API docs, ADHD design)
design/            # Design proposals (ADHD priorities, architecture, devops)
```

## Architecture decisions

- **Axum 0.8** web framework with Tokio async runtime
- **Repository trait** (`TaskRepository`) abstracts storage — currently in-memory, will be Notion API
- **AppState** is cloneable and holds `Arc<Config>`, `Arc<InMemoryTaskRepository>`, and server start time
- **AppError** maps to HTTP status codes (404, 400, 500) with JSON error bodies
- **Structured logging** via `tracing` — JSON format in production, pretty format in development
- **tower-http** middleware for request tracing, UUID request IDs, and CORS

## Domain model

The `Task` struct has fields designed for ADHD features from day one:
- `rejection_count` / `rejection_notes` — RSD-aware messaging
- `started_at` / `completed_at` — time blindness compensation
- `energy_required` (High/Medium/Low) — mood-based task selection
- `steps_completed` / `resume_count` — initiation and resume rewards
- `inline_steps` / `parent_task_id` / `sequence` — task decomposition

`WorkType` enum: Focus, Creative, Social, Independent.
`Mood` enum: Focused, Creative, Social, Tired, Stressed.

## CI pipeline

PR merges require all of these to pass:
1. **Style checks** — `cargo fmt --check` + `cargo clippy -- -D warnings`
2. **Unit tests** — `cargo test --lib --bins`
3. **Integration tests** — `cargo test --test '*'`
4. **Docker build** — multi-stage build + health endpoint smoke test

The pipeline uses path filtering (`src/`, `Cargo.toml`, `Cargo.lock`, `Dockerfile`) to skip unnecessary jobs. Cargo registry/target are cached by `Cargo.lock` hash.

Docker images are pushed to `ghcr.io` on main branch pushes and version tags via `docker-build-push.yml`.

## Environment variables

See `.env.example` for the full list. Key vars:
- `PORT` — server port (default: 8080)
- `RUST_LOG` — log filter (default: "info")
- `ENVIRONMENT` — "development" or "production" (affects log format)
- `NOTION_API_KEY` — Notion integration token (not yet consumed)
- `NOTION_DATABASE_ID` — target Notion database (not yet consumed)
- `ANTHROPIC_API_KEY` — Claude API key (not yet consumed)

## ADHD design philosophy

Read `design/adhd-priorities.md` for the full analysis. Key principles:

- **Zero-question intake:** AI infers task labels from natural language; user confirms via accept/reject, never open questions
- **RSD-aware messaging:** Every AI response must prevent shame/judgment; normalize task rejection
- **Initiation rewards:** Dopamine hit at task start, not just completion
- **Time blindness compensation:** Track estimated vs actual duration, compute per-user time multiplier

## Development environment

Use the devcontainer (`.devcontainer/`). It installs Rust, git hooks, GitHub CLI, and Docker-in-Docker. The `post-create.sh` script runs `cargo fetch` and installs hooks automatically.

## Code style

- Rust 2021 edition
- `cargo fmt` is the formatter — no custom rustfmt config
- `cargo clippy` with `-D warnings` — all warnings are errors
- Prefer `Arc` for shared state, `RwLock` for interior mutability
- Error handling via the `AppError` enum — don't use `.unwrap()` in production code
- Integration tests start their own server on an ephemeral port
