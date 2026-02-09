# Rust Architecture Proposal: hide-my-list Hello World

## Summary

This document proposes the architecture for the initial Rust implementation of hide-my-list. The goal is a minimal but structurally honest hello world: a running HTTP server that demonstrates the module layout, error handling patterns, configuration approach, and testing strategy that the full application will use. Every structural decision here is designed so that adding ADHD-focused features (task intake, AI-driven selection, personalized breakdowns, reward delivery) requires adding code, not restructuring it.

---

## 1. Rust Project Structure

### Crate Layout

Single-crate workspace to start. The binary lives in `src/main.rs`; all domain logic lives in library modules under `src/`. This keeps `cargo test --lib` fast and means integration tests in `tests/` can import the library crate directly.

```
hide-my-list/
  Cargo.toml
  Cargo.lock
  Dockerfile
  src/
    main.rs              # Entry point: config, server startup, graceful shutdown
    lib.rs               # Re-exports public modules
    config.rs            # Typed config from environment variables
    error.rs             # Application error types
    api/
      mod.rs             # Router construction
      health.rs          # GET /health
      conversation.rs    # POST /api/v1/conversation (hello world echo)
    domain/
      mod.rs             # Re-exports domain types
      task.rs            # Task, TaskStatus, WorkType, TimeEstimate, etc.
      preference.rs      # UserPreference, Mood, EnergyLevel
    storage/
      mod.rs             # Repository trait definitions
      memory.rs          # In-memory implementation
  tests/
    health_test.rs       # Integration test: health endpoint
    conversation_test.rs # Integration test: conversation echo
```

### Why This Layout

- **`api/`**: HTTP handlers are thin; they deserialize requests, call domain/storage logic, serialize responses. Adding `/api/v1/tasks` or `/api/v1/preferences` later means adding files here, not touching existing ones.
- **`domain/`**: Pure types and business logic with zero HTTP or storage awareness. This is where task lifecycle state machines, scoring algorithms, and preference matching will live. Unit-testable without spinning up a server.
- **`storage/`**: Trait-based abstraction. The in-memory implementation is real enough for tests and the hello world; swapping in Notion API calls or SQLite later means implementing the same trait.

### Dependencies

```toml
[package]
name = "hide-my-list"
version = "0.1.0"
edition = "2021"

[dependencies]
axum = "0.8"
tokio = { version = "1", features = ["full"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
tracing = "0.1"
tracing-subscriber = { version = "0.3", features = ["json", "env-filter"] }
uuid = { version = "1", features = ["v4", "serde"] }
chrono = { version = "0.4", features = ["serde"] }
tower-http = { version = "0.6", features = ["trace", "request-id", "cors"] }

[dev-dependencies]
reqwest = { version = "0.12", features = ["json"] }
tokio = { version = "1", features = ["full", "test-util"] }
```

**Rationale for each dependency:**

| Crate | Purpose | Why not alternatives |
|-------|---------|---------------------|
| axum | HTTP framework | Tower-native, excellent extractors, no macro magic, strong ecosystem |
| tokio | Async runtime | De facto standard, required by axum |
| serde / serde_json | Serialization | Universal Rust standard |
| tracing / tracing-subscriber | Structured logging | Async-aware, span propagation, JSON output for production |
| uuid | Request IDs, future task IDs | Standard UUID v4 generation |
| chrono | Timestamps | Serde integration, ISO 8601 formatting |
| tower-http | Middleware | Request ID propagation, CORS, request tracing |
| reqwest (dev) | Integration test HTTP client | Ergonomic async client |

**Intentionally deferred:** `anyhow` (we want typed errors), `thiserror` (only if error boilerplate becomes painful), `sqlx`/`notion-client` (no external storage yet).

---

## 2. Core Domain Types

These types mirror the Notion schema from the project docs but are defined as Rust enums and structs. They are designed to be extended without breaking existing code.

```rust
// domain/task.rs

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// The lifecycle status of a task.
/// Maps directly to the Notion "status" select field.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskStatus {
    Pending,
    InProgress,
    Completed,
    HasSubtasks,
}

/// What kind of work the task requires.
/// Used for mood-matching during task selection.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkType {
    Focus,
    Creative,
    Social,
    Independent,
}

/// Cognitive/physical energy needed for a task.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EnergyLevel {
    High,
    Medium,
    Low,
}

/// A task in the system. Fields match the Notion schema.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Task {
    pub id: Uuid,
    pub title: String,
    pub status: TaskStatus,
    pub work_type: WorkType,
    /// 0-100 scale indicating time sensitivity.
    pub urgency: u8,
    /// Estimated minutes to complete.
    pub time_estimate_minutes: u32,
    pub energy_required: EnergyLevel,
    pub created_at: DateTime<Utc>,
    pub completed_at: Option<DateTime<Utc>>,
    pub rejection_count: u32,
    pub rejection_notes: Vec<String>,
    pub ai_context: Option<String>,
    pub inline_steps: Option<String>,
    pub parent_task_id: Option<Uuid>,
    pub sequence: Option<u32>,
    pub progress_notes: Vec<String>,
}
```

```rust
// domain/preference.rs

use serde::{Deserialize, Serialize};

/// The user's current mood, parsed from natural language.
/// Drives work-type matching during task selection.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Mood {
    Focused,
    Creative,
    Social,
    Tired,
    Stressed,
}

/// User preferences that personalize task breakdowns.
/// Maps to the Notion user_preferences table.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UserPreference {
    pub user_id: String,
    pub preferred_beverage: Option<String>,
    pub comfort_spot: Option<String>,
    pub transition_ritual: Option<String>,
    pub focus_music: Option<String>,
    pub break_activity: Option<String>,
}
```

**Design notes:**
- All enums use `#[serde(rename_all = "snake_case")]` so they serialize to the same strings used in the Notion schema.
- `Task` has all fields from the Notion schema, even though the hello world won't populate most of them. This means code that creates or reads tasks always uses the real type.
- `Vec<String>` for rejection_notes and progress_notes (append-only logs) rather than a single string -- easier to work with in Rust.
- `Option<T>` for nullable fields (completed_at, parent_task_id, etc.).

---

## 3. API Endpoint Structure

### Router

```rust
// api/mod.rs

use axum::{Router, middleware};
use tower_http::trace::TraceLayer;
use tower_http::request_id::{SetRequestIdLayer, MakeRequestUuid};

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health::health_check))
        .nest("/api/v1", api_routes())
        .with_state(state)
        .layer(TraceLayer::new_for_http())
        .layer(SetRequestIdLayer::x_request_id(MakeRequestUuid))
}

fn api_routes() -> Router<AppState> {
    Router::new()
        .route("/conversation", post(conversation::converse))
        // Future:
        // .route("/tasks", get(tasks::list).post(tasks::create))
        // .route("/tasks/:id", get(tasks::get).patch(tasks::update))
        // .route("/preferences", get(preferences::get).put(preferences::update))
}
```

### Endpoint Contracts

#### GET /health

Returns structured health status. The CI/CD smoke test checks this endpoint.

```json
// 200 OK
{
  "status": "healthy",
  "version": "0.1.0",
  "uptime_seconds": 42
}
```

When dependencies are added later (Notion, Claude API), health check will include their status:

```json
{
  "status": "degraded",
  "version": "0.1.0",
  "uptime_seconds": 42,
  "dependencies": {
    "notion": { "status": "healthy", "latency_ms": 120 },
    "claude": { "status": "unhealthy", "error": "rate limited" }
  }
}
```

The application should continue serving requests even when dependencies are degraded. The `/health` response communicates the degradation but the HTTP status remains 200 unless the application itself is unable to process requests. A separate `/health/ready` can be added later for load-balancer readiness probes that should remove the instance from rotation.

#### POST /api/v1/conversation

The main chat interface. For the hello world, this echoes back with a greeting. The request/response structure is designed to grow into the full conversation system.

```json
// Request
{
  "message": "Hello, I need to review Sarah's proposal"
}

// Response (hello world)
{
  "response": "Hello! You said: \"Hello, I need to review Sarah's proposal\". I'm not smart enough to help with that yet, but I will be soon!",
  "intent": null,
  "task": null
}

// Response (future, after AI integration)
{
  "response": "Got it - focused work, ~30 min. Is there a deadline?",
  "intent": "add_task",
  "task": { "id": "...", "title": "Review Sarah's proposal", ... }
}
```

---

## 4. Data Layer: Repository Pattern

### Trait Definition

```rust
// storage/mod.rs

use crate::domain::task::Task;
use crate::error::AppError;
use uuid::Uuid;

#[async_trait::async_trait]
pub trait TaskRepository: Send + Sync {
    async fn create(&self, task: Task) -> Result<Task, AppError>;
    async fn get(&self, id: Uuid) -> Result<Option<Task>, AppError>;
    async fn list_pending(&self) -> Result<Vec<Task>, AppError>;
    async fn update(&self, task: Task) -> Result<Task, AppError>;
}
```

### In-Memory Implementation

```rust
// storage/memory.rs

use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;

pub struct InMemoryTaskRepository {
    tasks: Arc<RwLock<HashMap<Uuid, Task>>>,
}
```

**Why `Arc<RwLock<HashMap>>>`:** Multiple request handlers need concurrent read access; writes (task creation, status updates) are infrequent. `RwLock` allows many readers, one writer. `Arc` allows sharing across handler clones.

**Why not `DashMap`:** For the hello world traffic levels, `RwLock<HashMap>` is simpler, has no extra dependency, and is easy to reason about. If contention becomes measurable later, `DashMap` is a drop-in optimization.

### Future Storage Backends

The trait-based design means adding Notion or SQLite requires:
1. A new file (e.g., `storage/notion.rs`) implementing `TaskRepository`.
2. A config flag choosing which implementation to inject into `AppState`.
3. Zero changes to API handlers or domain logic.

---

## 5. Error Handling

### Custom Error Type

```rust
// error.rs

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde_json::json;

#[derive(Debug)]
pub enum AppError {
    /// Task not found by ID.
    NotFound(String),
    /// Request body failed validation.
    BadRequest(String),
    /// Internal error (storage failure, unexpected state).
    Internal(String),
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let (status, message) = match &self {
            AppError::NotFound(msg) => (StatusCode::NOT_FOUND, msg.clone()),
            AppError::BadRequest(msg) => (StatusCode::BAD_REQUEST, msg.clone()),
            AppError::Internal(msg) => {
                tracing::error!(error = %msg, "internal error");
                (StatusCode::INTERNAL_SERVER_ERROR, "internal error".to_string())
            }
        };

        let body = json!({
            "error": message,
        });

        (status, axum::Json(body)).into_response()
    }
}
```

**Design principles:**
- No panics in production code. All fallible operations return `Result<T, AppError>`.
- Internal error details are logged via tracing but NOT returned to the client (prevents information leakage).
- `AppError` implements `IntoResponse` so handlers can use `?` with axum's extraction.
- New error variants (e.g., `RateLimited`, `ExternalService`) can be added as dependencies are integrated.

---

## 6. Configuration

### Typed Config from Environment Variables

```rust
// config.rs

pub struct Config {
    /// HTTP port to bind to. Default: 8080.
    pub port: u16,
    /// Log level filter. Default: "info".
    pub log_level: String,
    /// Application environment. Default: "development".
    pub environment: String,
    // Future:
    // pub anthropic_api_key: Option<String>,
    // pub notion_api_key: Option<String>,
    // pub notion_database_id: Option<String>,
}

impl Config {
    pub fn from_env() -> Result<Self, AppError> {
        Ok(Config {
            port: std::env::var("PORT")
                .unwrap_or_else(|_| "8080".to_string())
                .parse()
                .map_err(|_| AppError::Internal("PORT must be a valid u16".into()))?,
            log_level: std::env::var("LOG_LEVEL")
                .unwrap_or_else(|_| "info".to_string()),
            environment: std::env::var("ENVIRONMENT")
                .unwrap_or_else(|_| "development".to_string()),
        })
    }

    pub fn is_production(&self) -> bool {
        self.environment == "production"
    }
}
```

**Why not `config` or `figment` crates:** The hello world has three config values. `std::env::var` with typed parsing is sufficient. If config complexity grows (TOML files, layered overrides, secrets injection), a config crate can be adopted then.

**Why `Option<String>` for future API keys:** Missing keys should not prevent the application from starting. The application starts degraded, and the health endpoint reports which services are unavailable. This prevents deployment failures when a non-critical secret is missing.

---

## 7. Reliability

### Graceful Shutdown

```rust
// main.rs (sketch)

#[tokio::main]
async fn main() {
    let config = Config::from_env().expect("invalid configuration");

    // Initialize structured logging
    init_tracing(&config);

    // Build application state
    let state = AppState::new(config);

    // Build router
    let app = api::router(state);

    // Bind listener
    let listener = tokio::net::TcpListener::bind(("0.0.0.0", config.port))
        .await
        .expect("failed to bind");

    tracing::info!(port = config.port, "server starting");

    // Serve with graceful shutdown on SIGTERM/SIGINT
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .expect("server error");

    tracing::info!("server shut down gracefully");
}

async fn shutdown_signal() {
    let ctrl_c = tokio::signal::ctrl_c();
    let mut sigterm = tokio::signal::unix::signal(
        tokio::signal::unix::SignalKind::terminate()
    ).expect("failed to install SIGTERM handler");

    tokio::select! {
        _ = ctrl_c => tracing::info!("received SIGINT"),
        _ = sigterm.recv() => tracing::info!("received SIGTERM"),
    }
}
```

**Why this matters for Docker:** `docker stop` sends SIGTERM. Without a graceful shutdown handler, in-flight requests are dropped. With it, the server finishes processing active requests before exiting. The 30-second timeout in the CI smoke test gives ample room.

### Structured JSON Logging

```rust
fn init_tracing(config: &Config) {
    let subscriber = tracing_subscriber::fmt()
        .with_env_filter(&config.log_level)
        .with_target(true)
        .with_thread_ids(true);

    if config.is_production() {
        subscriber.json().init();
    } else {
        subscriber.init();  // Pretty-printed for development
    }
}
```

Production logs emit JSON for ingestion by log aggregators. Development logs use the human-readable default. The `TraceLayer` middleware on the router automatically logs request method, path, status code, and latency for every request.

### Request ID Propagation

Every request gets a UUID via `SetRequestIdLayer`. This ID is:
- Available in handlers via `x-request-id` header extraction.
- Included in tracing spans so all log lines for a request can be correlated.
- Returned to the client in the response `x-request-id` header.

### Health Check Design

The health endpoint returns quickly (no external calls in the hello world). As dependencies are added, health checks should:
- Use cached status (check dependencies on a background timer, not per-request).
- Distinguish between "healthy" (all systems go), "degraded" (some dependencies down, core function works), and "unhealthy" (cannot serve requests).
- The Docker HEALTHCHECK and CI smoke tests hit `/health` and expect HTTP 200.

---

## 8. Testing Strategy

### Unit Tests (domain logic)

Location: inline `#[cfg(test)]` modules within `src/domain/*.rs`.

CI command: `cargo test --lib --bins`

```rust
// domain/task.rs

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn task_status_serializes_to_snake_case() {
        let json = serde_json::to_string(&TaskStatus::InProgress).unwrap();
        assert_eq!(json, "\"in_progress\"");
    }

    #[test]
    fn task_status_deserializes_from_snake_case() {
        let status: TaskStatus = serde_json::from_str("\"has_subtasks\"").unwrap();
        assert_eq!(status, TaskStatus::HasSubtasks);
    }
}
```

These tests validate:
- Serde serialization matches Notion schema field values.
- Domain type invariants (urgency range, time estimate validity).
- State transition rules (which status transitions are valid).

### Integration Tests (API endpoints)

Location: `tests/*.rs` files.

CI command: `cargo test --test '*'`

```rust
// tests/health_test.rs

#[tokio::test]
async fn health_endpoint_returns_200() {
    let app = build_test_app();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();

    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });

    let response = reqwest::get(format!("http://{}/health", addr))
        .await
        .unwrap();

    assert_eq!(response.status(), 200);

    let body: serde_json::Value = response.json().await.unwrap();
    assert_eq!(body["status"], "healthy");
}
```

```rust
// tests/conversation_test.rs

#[tokio::test]
async fn conversation_echoes_message() {
    let app = build_test_app();
    // ... setup listener and spawn server ...

    let client = reqwest::Client::new();
    let response = client
        .post(format!("http://{}/api/v1/conversation", addr))
        .json(&serde_json::json!({ "message": "hello" }))
        .send()
        .await
        .unwrap();

    assert_eq!(response.status(), 200);

    let body: serde_json::Value = response.json().await.unwrap();
    assert!(body["response"].as_str().unwrap().contains("hello"));
}
```

Integration tests use a helper function `build_test_app()` that constructs the router with in-memory storage and default config. This means tests are self-contained and don't need external services.

### Docker Smoke Test

The CI pipeline (`.github/workflows/pr-tests.yml`) builds the Docker image, starts a container, and hits `/health`. This validates:

1. The Dockerfile builds successfully (multi-stage build, binary runs on target platform).
2. The binary starts without panicking.
3. The `/health` endpoint responds with HTTP 200 within 30 seconds.

The smoke test does NOT test application logic -- that's what unit and integration tests are for. It validates the deployment artifact.

### Test pyramid summary

| Layer | What it tests | Speed | CI command |
|-------|--------------|-------|------------|
| Unit | Domain types, serde, business rules | Fast (ms) | `cargo test --lib --bins` |
| Integration | HTTP endpoints end-to-end | Medium (seconds) | `cargo test --test '*'` |
| Docker smoke | Binary starts, /health responds | Slow (30s) | Docker build + curl in CI |

---

## 9. What the Hello World Actually Does

### Scope

The hello world is a running, tested, deployable Rust HTTP server that demonstrates the full architecture. It is minimal but real -- no placeholder TODOs, no commented-out code, no aspirational modules.

### Concrete behavior

1. **Starts up**: Reads `PORT` and `LOG_LEVEL` from env (defaults to 8080 and info). Initializes structured logging. Prints startup banner to logs.

2. **Serves `/health`**: Returns JSON `{"status": "healthy", "version": "0.1.0", "uptime_seconds": N}`. Responds within milliseconds. Used by Docker HEALTHCHECK and CI smoke tests.

3. **Serves `POST /api/v1/conversation`**: Accepts JSON `{"message": "..."}`. Returns JSON `{"response": "Hello! You said: \"...\". I'm not smart enough to help with that yet, but I will be soon!", "intent": null, "task": null}`. Validates request body (returns 400 for missing/empty message).

4. **Logs all requests**: Method, path, status, latency, request ID -- structured JSON in production, pretty-printed in development.

5. **Shuts down gracefully**: On SIGTERM (from `docker stop`) or SIGINT (Ctrl+C), finishes in-flight requests and exits cleanly.

6. **Passes all CI checks**: `cargo fmt --check`, `cargo clippy -- -D warnings`, `cargo test --lib --bins`, `cargo test --test '*'`, Docker build and smoke test.

### What it does NOT do

- No AI integration (no Anthropic API calls).
- No Notion integration (no external database).
- No frontend (no static file serving).
- No task CRUD (domain types exist but are not wired to endpoints).
- No authentication (single-user MVP, deferred).

### Why this scope is right

The hello world establishes the foundation. Every subsequent feature -- task intake, AI labeling, mood-based selection, personalized breakdowns, reward delivery -- will be added by implementing domain logic, adding storage backends, and wiring new endpoints. The architecture does not need to change. The patterns for error handling, testing, configuration, and deployment are set.

---

## 10. Application State

```rust
// In api/mod.rs or a dedicated state.rs

use std::sync::Arc;
use std::time::Instant;
use crate::config::Config;
use crate::storage::memory::InMemoryTaskRepository;

#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Config>,
    pub started_at: Instant,
    pub task_repo: Arc<InMemoryTaskRepository>,
}

impl AppState {
    pub fn new(config: Config) -> Self {
        Self {
            config: Arc::new(config),
            started_at: Instant::now(),
            task_repo: Arc::new(InMemoryTaskRepository::new()),
        }
    }
}
```

State is shared via `Arc` (cheap clone for axum's handler model). The `started_at` field supports the health endpoint's `uptime_seconds`. The `task_repo` is ready to be used when task endpoints are added.

---

## 11. Dockerfile

```dockerfile
# Build stage
FROM rust:1-slim AS builder
WORKDIR /app
COPY Cargo.toml Cargo.lock ./
COPY src/ ./src/
RUN cargo build --release

# Runtime stage
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/target/release/hide-my-list /usr/local/bin/
EXPOSE 8080
ENV ENVIRONMENT=production
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD curl -sf http://localhost:8080/health || exit 1
CMD ["hide-my-list"]
```

Multi-stage build keeps the runtime image small (no Rust toolchain). The HEALTHCHECK directive lets Docker monitor the container and is used by the CI smoke test. `ca-certificates` is included for future HTTPS calls to external APIs (Claude, Notion).

---

## 12. Incremental Path to Full Features

This architecture supports adding features in this order, each building on the last:

| Phase | Feature | Files changed/added |
|-------|---------|-------------------|
| 1 | Hello world (this proposal) | All new files |
| 2 | Task CRUD endpoints | `api/tasks.rs`, wire `TaskRepository` to handlers |
| 3 | Static file serving (chat UI) | `api/mod.rs` (add static file layer), `web/` directory |
| 4 | Anthropic Claude integration | `ai/client.rs`, `ai/prompts.rs`, new config fields |
| 5 | Intent detection + task intake | `domain/intent.rs`, update `conversation.rs` |
| 6 | Task selection + mood matching | `domain/selection.rs`, scoring algorithm |
| 7 | Notion storage backend | `storage/notion.rs`, implement `TaskRepository` |
| 8 | User preferences | `api/preferences.rs`, `storage/` preference trait |
| 9 | Personalized breakdowns | `domain/breakdown.rs`, preference injection |
| 10 | Reward system | `rewards/engine.rs`, `rewards/delivery.rs` |
| 11 | Check-in timers | Server-sent events or WebSocket upgrade |

Each phase adds files. Existing files receive minimal changes (wiring new routes, adding config fields). The domain types, error handling, testing patterns, and deployment pipeline stay the same throughout.
