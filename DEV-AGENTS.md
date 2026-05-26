# DEV-AGENTS.md — hide-my-list

Dev agent context for Claude Code, Codex, human contributors.

## Architecture

- **Runtime**: Python + LangGraph app in Docker Compose
- **Storage**: Postgres (LangGraph checkpointer + reminder outbox + scheduler + private metadata) + Notion DB (tasks)
- **Messaging**: Signal via signal-cli bridge (infra-provided)
- **Scripts**: `scripts/` — Python migration helpers + ops CLIs
- **Docs**: `docs/` — spec contracts + contributor/CI guidance
- **Design**: `design/` — ADHD-informed design priorities

## Key Files

### Spec & Contract Files

These are the authoritative behavioral contracts. The Python implementation in `app/` conforms to them. Change one = change system behavior. Psych reviewer validates user-facing changes against ADHD research.

- `docs/ai-prompts/shared.md` — Base system prompt, intent dispatch, user preferences context, output/error/state handling (entry point for per-intent prompts)
- `docs/ai-prompts/intake.md` — Task Intake module (ADD_TASK): inference rules, sub-task generation, reminder detection
- `docs/ai-prompts/selection.md` — Task Selection module (GET_TASK): scoring weights, mood mapping
- `docs/ai-prompts/rejection.md` — Rejection Handling module (REJECT): shame-safe responses, escalation flow
- `docs/ai-prompts/cannot-finish.md` — Cannot Finish module (CANNOT_FINISH): progress gathering, sub-task creation
- `docs/ai-prompts/check-in.md` — Check-In Handling module (CHECK_IN): timing, shame-safe templates
- `docs/ai-prompts/breakdown.md` — Breakdown Assistance module (NEED_HELP): confidence detection, response levels
- `docs/architecture.md` — System architecture: container topology, LangGraph graph, reminder outbox, scheduled jobs
- `docs/task-lifecycle.md` — Task states: Pending → In Progress → Completed (with rejection/breakdown/reminder flows)
- `docs/notion-schema.md` — Notion database schema; `app/tools/notion.py` reads/writes against this
- `docs/user-interactions.md` — Conversation patterns + intent detection rules; `app/graph/routing.py` implements
- `docs/user-preferences.md` — Personalization behavior spec; user prefs stored in Postgres `user_prefs` table
- `docs/reward-system.md` — Multi-channel reward behavior spec; `app/tools/rewards.py` implements (v1: emoji + image)
- `design/adhd-priorities.md` — Core design principles grounded in ADHD research. **Critical: do not modify.**
- `setup/model-tiers.json` — Model tier source; `app/models.py` reads and validates at startup
- `scripts/notion-cli.sh` — Ops CLI: one-off Notion debugging. Production uses `app/tools/notion.py`.
- `scripts/user-time-context.sh` — Ops CLI: timezone helper for reminder parsing. Production uses `app/tools/time_context.py`.

### Python Runtime Files

The Python/LangGraph application. Safe to edit via PRs.

- `app/tools/notion.py` — Notion API client (9 verbs + health_check)
- `app/tools/signal_client.py` — Signal bridge async client
- `app/tools/reminders.py` — Reminder outbox CRUD
- `app/tools/rewards.py` — Reward delivery (emoji + image; v1 scope)
- `app/tools/ops_alerts.py` — Ops alert enqueue + drain
- `app/tools/time_context.py` — Timezone helper
- `app/tools/db.py` — Postgres connection + migration runner
- `app/graph/state.py` — LangGraph State TypedDict
- `app/graph/graph.py` — LangGraph graph definition
- `app/graph/routing.py` — Intent classification + conditional edges
- `app/graph/nodes/intake.py` — ADD_TASK intent node
- `app/graph/nodes/selection.py` — GET_TASK intent node
- `app/graph/nodes/chat.py` — CHAT intent node
- `app/graph/nodes/rejection.py` — REJECT intent node
- `app/graph/nodes/cannot_finish.py` — CANNOT_FINISH intent node
- `app/graph/nodes/need_help.py` — NEED_HELP intent node
- `app/graph/nodes/check_in.py` — CHECK_IN intent node
- `app/graph/nodes/complete.py` — COMPLETE intent node
- `app/graph/nodes/send.py` — Terminal send node
- `app/scheduler/scheduler.py` — APScheduler v3 wiring with PostgresJobStore
- `app/scheduler/jobs.py` — Declarative SCHEDULED_JOBS list + reconcile_jobstore; jobs: `reminder_dispatcher`, `notion_health`, `ops_alerts_drain`, `state_audit`, `check_in_dispatcher`, `weekly_recap`
- `app/scheduler/reminder_worker.py` — SELECT FOR UPDATE SKIP LOCKED worker
- `app/ingress/signal_listener.py` — WebSocket consumer routing to graph
- `app/prompts/` — Jinja2 prompt templates (`*.md.j2`) for each intent
- `app/models.py` — Model tier validation at startup; reads `setup/model-tiers.json`
- `app/main.py` — Entry point; LangSmith guard; production default `ENABLE_LANGGRAPH_PATH=true`
- `migrations/0001_initial.sql` — Initial schema: outbox, recent_outbound, ops_alerts_throttle
- `migrations/0002_reward_manifests.sql` — Reward manifests table
- `migrations/0003_ops_alerts.sql` — Ops alerts table
- `migrations/0004_user_prefs.sql` — User preferences table
- `tests/unit/` — Unit tests (no DATABASE_URL required)
- `tests/integration/` — Integration tests (require DATABASE_URL)
- `tests/spike/` — Durability spike tests
- `docs/python-rewrite/` — Python stack contributor docs and runbooks
- `docs/python-rewrite/rollback.md` — Cutover rollback runbook + forward cutover procedure
- `docs/python-rewrite/langgraph-semantics.md` — LangGraph durability spike findings
- `scripts/migrate_state_json.py` — One-shot OpenClaw → Postgres state migration; requires `--peer <E.164>`
- `docker/backup.sh` — Postgres pg_dump wrapper with retention policy
- `docker/Dockerfile` — Multi-stage Python 3.12-slim image for the app service
- `docker/compose.yaml` — Compose spec: `app` + `signal-cli` + `postgres:16-alpine`; `ENABLE_LANGGRAPH_PATH=true` default

### Infrastructure & CI Files

Support dev pipeline. Edit directly via PRs — any contributor or agent (Claude Code, Codex, etc.).

- `.github/workflows/` — GitHub Actions workflow definitions
- `.github/actions/` — Composite actions used by workflows
- `.github/actions/review-claude-run/` — Direct-`docker run` composite invoking Claude Code against the LiteLLM Anthropic endpoint; v2 pipeline single-writer fixer (fresh-Claude fallback path for human-authored PRs)
- `.github/actions/review-codex-resume/` — Composite that resumes the original `resolve-issue` Codex author session as the v2 fixer; bind-mounts the persisted session into `/home/ci/.codex` and runs `codex exec resume --last`
- `.github/actions/review-claude-resume/` — Composite that resumes the original `resolve-issue` Claude Code author session as the v2 fixer; bind-mounts the persisted session into `/home/ci/.claude` and runs `claude --continue`
- `.github/scripts/review/prompts/fixer-claude-smoke.md` — Prompt for the Claude fixer auth/IO smoke test
- `.github/scripts/review/prompts/fixer-resume.md` — Prompt loaded by both resume actions; the resumed author already has full authoring context, so the prompt only hands over reviewer artifacts and reasserts the `.git/`-don't-touch + output-contract constraints
- `.github/workflows/review-fixer-claude-smoke.yml` — Pre-merge smoke test exercising the Claude fixer container path on PRs touching that path
- `.github/workflows/review-fixer-resume-smoke.yml` — Pre-merge smoke test exercising the resume-fixer dispatch logic (`scripts/test-ci-session-store.sh`); full cross-container resume validated by the first multi-cycle review on a `resolve-issue` PR
- `.github/ci/prompts/codex-resolve-issue.md` — Codex author prompt invoked by the `codex` agent path in `resolve-issue`; carries the `Author-Session: codex/${RUN_ID}` PR-body trailer contract
- `.github/ci/prompts/claude-resolve-issue.md` — Claude Code author prompt invoked by the `claude` agent path in `resolve-issue`; carries the `Author-Session: claude/${RUN_ID}` PR-body trailer contract
- `.github/scripts/review/render-finalize-comment.sh` — Renders and posts the operator-facing "Agent Review Summary" merge-decision comment for `review-finalize.yml`; branches on verdict category (`go`, `reviewer_blockers`, `pipeline_error`, `cycle_capped`, `inherited`)
- `.github/ci/caveman-rules.md` — Canonical CI-only caveman prompt contract prepended by `review-codex-run`, `review-claude-run`, `review-codex-resume`, and `review-claude-resume`
- `docs/agentic-pipeline-learnings.md` — Prescriptive review/CI pipeline contract + guardrail doc
- `scripts/create-deduped-workflow-failure-issue.sh` — Creates/reuses canonical deduplicated GH Actions failure issue for diagnosis workflow
- `scripts/check-doc-links.sh` — Internal doc link validator for local hooks + CI doc checks
- `scripts/ci-session-store.sh` — Path-naming, pack/unpack, and trailer-parse helper for the per-(agent, issue, run-id) author-session store (job-local under `${RUNNER_TEMP}/ci-sessions/<agent>/<issue>/<run-id>/`); used by both `resolve-issue` (pack + upload) and v2 review-fixer (download + unpack)
- `scripts/test-ci-session-store.sh` — Self-contained unit tests for `ci-session-store.sh`; invoked by `review-fixer-resume-smoke.yml`
- `scripts/run-required-checks.sh` — Canonical local/CI runner for required script, doc, and workflow validations (no OpenClaw config mode)
- `scripts/security-update.sh` — Security update automation
- `scripts/validate-gh-cli-usage.sh` — GitHub CLI workflow usage validation
- `scripts/validate-pr-tests-workflow.sh` — PR Tests workflow actionlint/setup-order validation
- `scripts/validate-workflow-refs.sh` — Workflow reference validation
- `scripts/validate-mermaid.sh`, `scripts/lint-mermaid-rendering.sh` — Diagram validation
- `setup/model-tiers.json` — Repo metadata mapping expensive, medium, and cheap model tiers; read by `app/models.py` at startup
- `pyproject.toml` — Python 3.12 dependency manifest for the LangGraph stack; runtime and dev deps pinned by version
- `.github/workflows/python-validation.yml` — Required CI gate: ruff + mypy + pytest-unit on every PR touching Python source files

## Safety

- **NEVER touch firewall rules.** Critical security. No exceptions.
- Don't exfiltrate data.
- **Don't leak private examples in GitHub issues, PRs, commits, or review-pipeline artifacts.** This is a public repo. Do not name real people, real recipient phone numbers, real reminder content, real Notion page titles, or real personal events in issues, PR descriptions, commit messages, code comments, or review-pipeline artifacts (review comments, fix-attempt summaries). State the technical problem and desired fix; use placeholder content (`<page_id>`, `<recipient>`, `"Test message"`, etc.). If a specific date/time is load-bearing, keep the date but omit the personal context.
- `trash` > `rm`.

### Prompt & Spec Files — Present Tense Only

The spec and contract files listed in "Spec & Contract Files" above define system behavior. Write them like a system prompt, not a changelog.

Rules:

- **Present tense only.** Describe how the system behaves *right now*. No "now does X", "previously Y", "used to Z", "still [uses old approach]" (historical comparisons). Present-state uses of "still" ("while still running", "agents still control") are fine.
- **No `(Issue #N)` / `(PR #N)` / `(#N)` suffixes** on section headers, list items, or callouts. Issue and PR numbers go in the commit message, the PR body, the linked issue itself — not in the runtime prompt.
- **Replace, don't diff.** When behavior changes, rewrite the section. Don't keep before/after framing or "Why we changed this:" notes in the spec — that belongs in the commit message.
- **No roadmaps in spec files.** In-flight tracking belongs in GitHub issues/projects.
- **Rationale belongs in present tense.** "Why this design:" framing is fine — explain the constraint that *currently* governs the choice.

This rule applies to: `docs/ai-prompts/*.md`, `docs/architecture.md`, `docs/task-lifecycle.md`, `docs/notion-schema.md`, `docs/user-interactions.md`, `docs/user-preferences.md`, `docs/reward-system.md`, `design/adhd-priorities.md`.

It does **not** apply to `docs/agentic-pipeline-learnings.md` or other contributor/CI-only guidance, which legitimately carry historical context.

## Review Pipeline

PRs reviewed by multi-agent review pipeline (Codex reviewers + fixer in v2). Roles same in both versions; orchestration differs.

Reviewers handle Markdown spec changes and Python source changes (`app/`, `migrations/`, `tests/`). The classifier (`review-classify` action) detects which file classes are present and routes accordingly. Python source files (`app/**/*.py`, `migrations/*.sql`, `tests/**/*.py`) always trigger security review; `app/prompts/*.md.j2` triggers prompt + psych review.

**Reviewer roles**:
1. Design Review — validates intent + design quality; runs docs-as-spec consistency check on spec-critical changes; evaluates Python module design, async correctness, LangGraph node patterns, constrained-tool-surface invariant
2. Security & Infrastructure Review — script safety, credential handling, workflow permissions, GH Actions/runtime correctness; Python: SQL injection (parameterised queries), secrets in env, no shell-out from app code, constrained-tool-surface invariant
3. Psych Research Review — validates against ADHD clinical research; evaluates `app/prompts/*.md.j2` for banned-phrase regex + shame-safety contract
4. Prompt Engineering Review — validates prompt clarity, constraints, cross-prompt consistency; evaluates `app/prompts/*.md.j2` for prompt-parity contract (section headings from `docs/ai-prompts/` source present in template)
5. Documentation Consistency Review — contradictions, stale refs, cross-doc consistency; includes `docs/python-rewrite/*.md` and Python Runtime Files listed in this file
6. Judge / Merge Decision — synthesizes all reviews into verdict

Lives in `.github/workflows/review-entry.yml`, dispatches `review-pipeline.yml` (orchestrator) → `review-reviewer.yml` (matrix) → `review-fixer.yml` → `review-judge.yml` → `review-finalize.yml`. Judge = deterministic Node script (`.github/scripts/review/aggregate.mjs`) with `permissions: contents: read` — cannot push by construction. Fixer runs after reviewers, before judge; pushes new commit first, then claims that SHA on `review/pipeline` immediately after push (GitHub rejects status for unpublished commit); only stage with write permission. The fixer also attempts `git merge --no-commit --no-ff origin/main` before invoking the agent so AI-authored PRs stay mergeable without a human in the loop — clean merges seal on the host, conflicts go through the agent for marker resolution, unresolved conflicts abort the merge and label `needs-human-review`. Verdicts binary **GO** / **NO-GO**; NO-GO labels PR `needs-human-review`, stops without closing or auto-creating issues. Reviewer prompts = standalone files in `.github/scripts/review/prompts/`. See `docs/agentic-pipeline-learnings.md` §1.4 + §1.5 for design decisions.

### Author-resume in the fixer stage

`resolve-issue` accepts both **Codex** and **Claude Code** as first-class authors (selected per run via `/autoresolve <agent>` comment, `agent:codex` / `agent:claude` issue label, or repo default). The author's session state is bind-mounted from a job-local directory during the author run, then packed (`scripts/ci-session-store.sh pack`) and uploaded as the `author-session-<agent>-<run-id>` workflow artifact so it can travel between ephemeral runners. The author writes an `Author-Session: <agent>/<run-id>` trailer into the PR body.

When `review-fixer.yml` runs, `Parse Author-Session trailer` extracts `<agent>/<run-id>`, `Download author session artifact` (`actions/download-artifact@v4` with `run-id` cross-run lookup; requires `actions: read`) fetches the tarball from the original `resolve-issue` run, and `Detect author session for resume` unpacks + validates it. Three-way dispatch:
- **`codex-resume`** → `review-codex-resume` action runs `codex exec resume --last` against the unpacked session
- **`claude-resume`** → `review-claude-resume` action runs `claude --continue`
- **`fallback`** → existing `review-claude-run` (fresh Claude session) — used for human-authored PRs and any case where the trailer is absent, the artifact is missing/expired, or unpack fails validation

The resumed author re-enters the same conversation it had while authoring, this time with `${REVIEWER_ARTIFACTS_DIR}` available in scope. Because it has full context for the choices it originally made, it can revisit those choices instead of patching surface-level symptoms. Reviewers + judge always run regardless of dispatch mode — backstop catches anything the resumed author misses.

Symmetric two-agent support means the fixer must understand both `codex exec resume` and `claude --continue` semantics; `fixer-resume.md` is the shared prompt loaded by both resume actions.

### Review prompt file architecture

Reviewer prompts (`.github/scripts/review/prompts/{design,security,psych,docs,prompt}.md`) **self-contained** — each reviewer loads only its own `${role}.md` at runtime. Codex CLI doesn't support markdown includes.

Constraint applies to all reviewers → add to each prompt file individually. Use identical wording across files unless structure requires different phrasing (e.g., inline JSON placeholder vs. prose). Same applies to `fixer.md` — loaded independently.

"Sibling files with shared contract, loaded independently" pattern recurs throughout repo (e.g., spec files). Editing one file in group → check if siblings need same change.

## When Making Changes

- Spec docs define agent behavior — changing those docs changes system behavior; reviewed as behavioral changes
- Psych reviewer validates user-facing changes against ADHD research
- `config_only` infra/CI changes skip psych review automatically; prompt-bearing reviewer/config markdown follows specialist review path
- All changes go through PR with full review pipeline
