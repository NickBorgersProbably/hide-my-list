# DevOps & Infrastructure Strategy

## Overview

This document defines the DevOps strategy for hide-my-list, covering containerization, CI/CD, health checks, AI-powered diagnostics, monitoring, and deployment. The design builds on the existing GitHub Actions pipelines, devcontainer setup, and AI-driven workflow diagnostics already in the repository.

---

## 1. Dockerfile (Multi-Stage Build)

A multi-stage build keeps the final image small and secure while leveraging Rust's single-binary output.

```dockerfile
# ---- Builder Stage ----
FROM rust:1.84-slim AS builder

WORKDIR /app

# Cache dependencies by copying manifests first
COPY Cargo.toml Cargo.lock ./
RUN mkdir src && echo 'fn main() {}' > src/main.rs
RUN cargo build --release && rm -rf src

# Build the real application
COPY src/ src/
COPY web/ web/
COPY static/ static/
RUN touch src/main.rs && cargo build --release

# ---- Runtime Stage ----
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for security
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --shell /bin/sh --create-home appuser

WORKDIR /app

# Copy the compiled binary
COPY --from=builder /app/target/release/hide-my-list /app/hide-my-list

# Copy static assets
COPY web/ /app/web/
COPY static/ /app/static/

# Set ownership
RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8080

# Built-in health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

ENTRYPOINT ["/app/hide-my-list"]
```

### Design Decisions

- **rust:1.84-slim** builder: matches the Rust version in `.devcontainer/Dockerfile` (which uses `mcr.microsoft.com/devcontainers/rust:1`). Slim variant reduces build time.
- **Dependency caching layer**: the dummy `main.rs` trick caches all dependency compilation. Only application code changes trigger a full rebuild.
- **debian:bookworm-slim** runtime: minimal footprint (~80MB), provides libc compatibility, includes package manager for `ca-certificates` (needed for HTTPS to Notion and Claude APIs).
- **Non-root user**: `appuser` with UID 1000 -- prevents container escape privilege escalation.
- **HEALTHCHECK**: the existing CI pipelines (`pr-tests.yml:194`, `docker-build-push.yml:156`) already rely on `curl http://localhost:8080/health` for smoke tests. Building the health check into the Dockerfile ensures consistency.

---

## 2. Docker Compose (Local Development)

```yaml
# docker-compose.yml
services:
  app:
    build:
      context: .
      dockerfile: Dockerfile
    ports:
      - "8080:8080"
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - NOTION_API_KEY=${NOTION_API_KEY}
      - NOTION_DATABASE_ID=${NOTION_DATABASE_ID}
      - NOTION_PREFERENCES_DB_ID=${NOTION_PREFERENCES_DB_ID}
      - RUST_LOG=hide_my_list=debug,tower_http=debug
      - LOG_FORMAT=json
    volumes:
      - ./web:/app/web:ro
      - ./static:/app/static:ro
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 10s
      timeout: 3s
      start-period: 5s
      retries: 3
```

### Design Decisions

- **Volume mounts** for `web/` and `static/` allow live-editing frontend assets without rebuilding.
- **RUST_LOG** set to debug for local development; JSON log format for consistency with production.
- **Environment variables** passed from `.env` file (not committed to git).
- Future services (Notion mock for offline testing, Prometheus for local metrics) can be added as additional compose services.

---

## 3. Issue #15 Resolution

**Issue**: [#15](https://github.com/NickBorgers/hide-my-list/issues/15) -- "Workflow Failure: PR Tests run -- Rust workflow on non-Rust project"

**Root Cause**: The CI/CD pipelines in `.github/workflows/pr-tests.yml` and `.github/workflows/docker-build-push.yml` were correctly designed for a Rust project, but no `Cargo.toml` exists yet.

**Resolution**: Creating the Rust project structure (`Cargo.toml`, `src/main.rs`) will fix this issue. The workflows are already correctly configured:

- `pr-tests.yml` uses `dorny/paths-filter@v3` to detect changes in `src/**`, `Cargo.toml`, and `Cargo.lock` -- it will trigger automatically when these files exist.
- `docker-build-push.yml` already has a guard: `if: hashFiles('Cargo.toml') != ''` (line 28) -- it correctly skips Rust tests when no Cargo.toml exists.
- The git hooks (`.githooks/pre-commit`, `.githooks/pre-push`) already have `if [ ! -f "Cargo.toml" ]; then exit 0; fi` guards.

**No workflow changes needed**. The existing CI/CD is forward-compatible. Once `Cargo.toml` is committed, issue #15 resolves itself.

---

## 4. Health Check Design

### Endpoint: `GET /health`

```json
{
  "status": "healthy",
  "version": "0.1.0",
  "uptime_seconds": 3742,
  "checks": {}
}
```

### Implementation Approach

The health endpoint should be a dedicated Actix-web (or Axum) handler that:

1. **Always responds fast** -- no external calls in the basic check.
2. **Returns structured JSON** so the Dockerfile HEALTHCHECK, CI smoke tests, and monitoring can parse it.
3. **Includes version** -- compiled from `CARGO_PKG_VERSION` or a build-time env var.
4. **Tracks uptime** -- captured via `std::time::Instant` at startup.

### Future Extension: Dependency Checks

When external integrations are added, the `checks` object will include them:

```json
{
  "status": "degraded",
  "version": "0.3.0",
  "uptime_seconds": 86412,
  "checks": {
    "notion_api": {
      "status": "healthy",
      "latency_ms": 142
    },
    "claude_api": {
      "status": "unhealthy",
      "error": "rate_limited",
      "last_success": "2025-01-15T10:32:00Z"
    }
  }
}
```

The overall `status` field follows this logic:
- `"healthy"`: all checks pass (or no checks configured)
- `"degraded"`: some non-critical checks fail
- `"unhealthy"`: critical checks fail

Docker and orchestrators use the HTTP status code: 200 for healthy/degraded, 503 for unhealthy.

---

## 5. AI-Powered Diagnostics Strategy

This is the core differentiator: designing every operational aspect so that AI (Claude) can diagnose and resolve issues with minimal human intervention. The existing `claude-diagnose-workflow-failure.yml` demonstrates this pattern for CI -- we extend it to runtime.

### 5.1 Structured JSON Logging

Use `tracing` + `tracing-subscriber` with JSON formatter so every log line is machine-parseable.

```rust
// In main.rs initialization
use tracing_subscriber::{fmt, EnvFilter};

tracing_subscriber::fmt()
    .json()
    .with_env_filter(EnvFilter::from_default_env())
    .with_target(true)
    .with_thread_ids(true)
    .with_span_events(fmt::format::FmtSpan::CLOSE)
    .init();
```

Example log output:

```json
{
  "timestamp": "2025-01-15T10:32:14.123Z",
  "level": "ERROR",
  "target": "hide_my_list::api::chat",
  "message": "Claude API call failed",
  "request_id": "req_a1b2c3d4",
  "error_category": "external_service",
  "error_code": "claude_rate_limit",
  "retry_count": 2,
  "user_intent": "task_intake",
  "span": {
    "name": "handle_chat_message",
    "duration_ms": 4521
  }
}
```

### 5.2 Request ID Propagation

Every inbound HTTP request gets a UUID assigned in middleware, carried through all spans:

```rust
// Middleware assigns request_id
let request_id = Uuid::new_v4().to_string();
// Stored in tracing span
let span = tracing::info_span!("http_request", request_id = %request_id, method = %method, path = %path);
```

This allows correlating all log lines for a single user interaction, which is critical for AI diagnosis -- when Claude analyzes logs, it can follow a single request through the entire system.

### 5.3 Error Context Enrichment

Every error should carry enough structured context for AI to diagnose without guessing:

```rust
#[derive(Debug)]
struct DiagnosticError {
    category: ErrorCategory,    // external_service, internal, validation, configuration
    code: String,               // machine-readable: "claude_rate_limit", "notion_auth_expired"
    message: String,            // human-readable description
    context: serde_json::Value, // arbitrary structured context
    recoverable: bool,          // can the system retry?
    suggested_action: String,   // what should be done: "retry_with_backoff", "check_api_key"
}

enum ErrorCategory {
    ExternalService,  // Notion/Claude API failures
    Internal,         // bugs, panics, logic errors
    Validation,       // bad user input
    Configuration,    // missing env vars, bad config
}
```

The `suggested_action` field is designed specifically for AI consumption: when Claude analyzes error logs, this field tells it what class of remediation to attempt.

### 5.4 Consistent Error Categorization

All errors use a fixed taxonomy so AI can aggregate and pattern-match:

| Category | Examples | AI Action |
|----------|----------|-----------|
| `external_service` | Claude API timeout, Notion 429 | Check service status, retry with backoff |
| `internal` | Panic, logic error, unexpected state | Examine stack trace, check recent deploys |
| `validation` | Bad chat message format | Check client-side validation |
| `configuration` | Missing `ANTHROPIC_API_KEY` | Check environment variables |

### 5.5 Extending CI Diagnostics to Runtime

The existing `claude-diagnose-workflow-failure.yml` pattern -- detect failure, collect logs, have Claude analyze and create issues -- extends to runtime:

**Phase 1 (Current)**: CI pipeline failures diagnosed by Claude, issues created automatically. Already implemented and working.

**Phase 2 (Near-term)**: Runtime log analysis. A scheduled GitHub Action or external cron job:
1. Pulls recent structured logs from the Docker host.
2. Filters for ERROR-level entries or anomalous patterns.
3. Passes them to Claude with the same diagnostic prompt pattern used in `claude-diagnose-workflow-failure.yml`.
4. Creates GitHub issues for configuration or code problems; ignores transient external failures.

**Phase 3 (Future)**: Real-time alerting. A sidecar or log shipper watches for error rate spikes and triggers Claude diagnosis immediately, potentially with auto-remediation (restart container, roll back deployment).

### 5.6 Metrics to Expose

| Metric | Type | Purpose |
|--------|------|---------|
| `http_requests_total` | Counter | Request volume by path, method, status |
| `http_request_duration_seconds` | Histogram | Latency distribution by endpoint |
| `claude_api_calls_total` | Counter | AI API usage by intent type |
| `claude_api_duration_seconds` | Histogram | AI response latency |
| `notion_api_calls_total` | Counter | Notion API usage by operation |
| `notion_api_duration_seconds` | Histogram | Notion response latency |
| `task_lifecycle_transitions_total` | Counter | Transitions by from_status/to_status |
| `active_tasks_gauge` | Gauge | Current tasks by status |
| `error_total` | Counter | Errors by category and code |

These metrics serve two purposes:
1. Traditional dashboarding (Prometheus/Grafana).
2. AI diagnosis input -- Claude can compare current error rates against baseline to detect regressions.

### 5.7 AI-Driven Rollback Decisions

Building toward automated rollback requires:

1. **Version tagging**: every Docker image tagged with `main-<sha7>` (already configured in `docker-build-push.yml` line 87). The health endpoint includes the version.

2. **Deployment markers in logs**: when a new version starts, it emits a structured log entry:
   ```json
   {
     "level": "INFO",
     "message": "Application started",
     "version": "0.2.1",
     "git_sha": "abc1234",
     "previous_version": "0.2.0",
     "deployment_marker": true
   }
   ```

3. **Error rate comparison**: after deployment, compare error rates in the window `[deploy_time, deploy_time + 10min]` against the previous version's baseline. If error rate exceeds 2x baseline, flag for rollback.

4. **Rollback mechanism**: pull and run the previous known-good image tag from GHCR. The `verify-pushed-image` job in `docker-build-push.yml` ensures every pushed image is smoke-tested, so rolling back to any pushed image is safe.

5. **AI decision loop**: Claude receives the error rate comparison, deployment marker, and recent error logs. It decides:
   - **Continue**: error rate within normal range.
   - **Alert**: elevated errors but not critical -- create a GitHub issue.
   - **Rollback**: critical error spike -- execute rollback to previous image.

---

## 6. Backup Strategy

### 6.1 Application Data

**Current state (MVP)**: in-memory task state. No backup needed -- state is recreated on restart.

**When persistent storage is added**:
- Notion is the primary data store (as defined in `docs/architecture.md`). Notion handles its own backups.
- If a local database is added later, Docker volume backups to cloud storage (e.g., a nightly cron job that `docker cp`s the volume and uploads to S3/GCS).

### 6.2 Configuration Backup

- All configuration is in git: workflow files, Dockerfile, devcontainer config, docs.
- Environment variables (API keys) are stored in GitHub Secrets and the deployment host's environment -- these should be documented in a secure location (not git).
- The `.env.example` file (to be created) documents required variables without values.

### 6.3 Docker Image Backup

- All images are pushed to GHCR (`ghcr.io/nickborgers/hide-my-list`) via `docker-build-push.yml`.
- GHCR retains all tagged images. Version tags (`v*`) provide immutable release snapshots.
- The `latest` tag always points to the most recent main branch build.

---

## 7. Deployment Pipeline

### Pipeline Stages

```
Local Development
    |
    v
Git Push (pre-commit: fmt check, pre-push: clippy + tests)
    |
    v
PR Created -> PR Tests Workflow
    |           |-- Detect Changes (path filtering)
    |           |-- Style Checks (cargo fmt, clippy)
    |           |-- Unit Tests (cargo test --lib --bins)
    |           |-- Integration Tests (cargo test --test '*')
    |           |-- Docker Build Validation + Smoke Test
    |           |-- All Tests Gate
    |
    v
PR Tests Pass -> Claude Code Review Workflow
    |               |-- Fix Test Failures (up to 3 attempts)
    |               |-- Design Review
    |               |-- Code Review
    |               |-- Test Review
    |               |-- Concurrency Review
    |               |-- Docs Review
    |               |-- Merge Decision (GO/NO-GO)
    |
    v
Merge to Main -> Build and Push Docker Image Workflow
    |               |-- Run Rust Tests
    |               |-- Build Multi-Arch Image (amd64 + arm64)
    |               |-- Push to GHCR
    |               |-- Verify Pushed Image (pull + smoke test)
    |
    v
Deploy to Docker Host (to be added)
    |               |-- Pull new image from GHCR
    |               |-- Stop old container
    |               |-- Start new container
    |               |-- Health check verification
    |               |-- Deployment marker log entry
    |
    v
Post-Deploy Monitoring
                    |-- Error rate comparison
                    |-- AI-powered diagnosis if issues detected
                    |-- Auto-rollback if critical failure
```

### What Already Exists

The existing pipelines cover the first three stages comprehensively:

- **`pr-tests.yml`**: path-filtered Rust checks with Docker smoke test.
- **`claude-code-review.yml`**: 5-agent review pipeline with automated fix attempts, sequential review chain (design -> code -> test -> concurrency -> docs), merge decision, and commit status updates.
- **`docker-build-push.yml`**: multi-arch build, GHCR push, post-push verification.
- **`claude-diagnose-workflow-failure.yml`**: AI-powered failure diagnosis for all workflows.
- **`claude.yml`**: interactive Claude agent for issues and PR comments.

### What Needs to Be Added

**Deployment to Docker Host**: a new workflow or extension to `docker-build-push.yml` that, after the `verify-pushed-image` job succeeds:

1. SSHs to the target Docker host (using a GitHub Secret for SSH key).
2. Pulls the new image.
3. Stops the old container gracefully (SIGTERM with timeout).
4. Starts the new container with environment variables.
5. Waits for health check to pass.
6. Emits deployment marker to logs.
7. Monitors error rate for 10 minutes post-deploy.

This can be implemented as a `deploy` job in `docker-build-push.yml` with:
```yaml
deploy:
  name: Deploy to Production
  needs: verify-pushed-image
  if: github.ref == 'refs/heads/main'
  runs-on: ubuntu-latest
  environment: production  # Requires manual approval via GitHub Environments
  steps:
    - name: Deploy via SSH
      uses: appleboy/ssh-action@v1
      with:
        host: ${{ secrets.DEPLOY_HOST }}
        username: ${{ secrets.DEPLOY_USER }}
        key: ${{ secrets.DEPLOY_SSH_KEY }}
        script: |
          docker pull ghcr.io/nickborgers/hide-my-list:latest
          docker stop hide-my-list || true
          docker rm hide-my-list || true
          docker run -d --name hide-my-list \
            --restart unless-stopped \
            -p 8080:8080 \
            --env-file /opt/hide-my-list/.env \
            ghcr.io/nickborgers/hide-my-list:latest
          # Wait for health
          for i in $(seq 1 30); do
            if curl -sf http://localhost:8080/health; then break; fi
            sleep 1
          done
```

---

## 8. Monitoring & Alerting

### 8.1 Prometheus Metrics Endpoint

`GET /metrics` exposes Prometheus-compatible metrics using the `prometheus` crate:

```
# HELP http_requests_total Total HTTP requests
# TYPE http_requests_total counter
http_requests_total{method="POST",path="/api/chat",status="200"} 1423
http_requests_total{method="GET",path="/health",status="200"} 8592

# HELP http_request_duration_seconds HTTP request duration
# TYPE http_request_duration_seconds histogram
http_request_duration_seconds_bucket{path="/api/chat",le="0.1"} 312
http_request_duration_seconds_bucket{path="/api/chat",le="0.5"} 891
http_request_duration_seconds_bucket{path="/api/chat",le="1.0"} 1201

# HELP task_lifecycle_transitions_total Task state transitions
# TYPE task_lifecycle_transitions_total counter
task_lifecycle_transitions_total{from="pending",to="in_progress"} 47
task_lifecycle_transitions_total{from="in_progress",to="completed"} 41
```

### 8.2 Log Aggregation

**Phase 1 (MVP)**: Docker container logs written to stdout in JSON format. Collected via `docker logs` or Docker logging drivers.

**Phase 2**: If scaling beyond a single host, ship logs to a centralized store (Loki, CloudWatch, or a simple log file on the host with rotation).

**Phase 3**: Structured log queries for AI diagnosis -- pull logs by `request_id`, `error_category`, or time window.

### 8.3 AI Diagnosis in the Alerting Pipeline

The alerting pipeline integrates AI at two levels:

**Level 1: CI/CD (already implemented)**
- `claude-diagnose-workflow-failure.yml` automatically analyzes workflow failures.
- Classifies failures as TEST_FAILURE, CONFIG_FAILURE, INFRASTRUCTURE_FAILURE, or ACTIONS_FAILURE.
- Only creates issues for actionable problems (ACTIONS_FAILURE).
- Transient external issues (502s, cache failures) are correctly identified and ignored.

**Level 2: Runtime (to be built)**
- Periodic or threshold-triggered log analysis.
- Claude receives recent error logs + metrics snapshot.
- Same classification pattern: is this a code bug, configuration issue, external service problem, or transient?
- Code bugs and configuration issues -> GitHub issue created.
- External service problems -> logged but no issue (self-resolving).
- Critical failures -> rollback decision.

**Alert Flow**:
```
Error detected (rate spike or specific error pattern)
    |
    v
Collect context: recent logs, metrics, deployment info
    |
    v
Claude analyzes with diagnostic prompt
    |
    v
Classification:
    |-- Code bug -> Create GitHub issue
    |-- Config issue -> Create GitHub issue + suggest env var fix
    |-- External service -> Log, no action
    |-- Critical regression -> Trigger rollback
```

---

## Summary: What Ships with Hello World

For the initial Rust hello world implementation, the following DevOps artifacts should be created:

| Artifact | Purpose |
|----------|---------|
| `Cargo.toml` | Resolves issue #15, enables all existing CI pipelines |
| `Dockerfile` | Multi-stage build as described in Section 1 |
| `docker-compose.yml` | Local development environment |
| `GET /health` handler | Health check endpoint as described in Section 4 |
| Structured logging init | `tracing` + `tracing-subscriber` JSON setup |
| Request ID middleware | UUID per request, propagated through spans |

Everything else (metrics endpoint, deployment automation, AI runtime diagnosis, rollback automation) builds incrementally on this foundation.
