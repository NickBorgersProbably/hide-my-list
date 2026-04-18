# DEV-AGENTS.md — hide-my-list

Development agent context for Claude Code, Codex, and human contributors. The OpenClaw runtime agent's instructions are in `AGENTS.md` — edits to that file change application behavior.

## Architecture

- **Runtime**: OpenClaw agent (no standalone server)
- **Storage**: Notion database via API
- **Scripts**: `scripts/` — Notion CLI helpers and infrastructure tooling
- **Docs**: `docs/` — mostly runtime behavior specs, plus contributor/CI guidance where explicitly noted
- **Design**: `design/` — ADHD-informed design priorities and principles
- **OpenClaw integration**: See `docs/openclaw-integration.md` for how this maps to the platform

## Key Files

### OpenClaw Prompt & Spec Files

These files define how the OpenClaw agent behaves — they *are* the application. Changing one changes the agent.

- `AGENTS.md` — OpenClaw runtime agent instructions (bootstrap file, auto-loaded by OpenClaw)
- `SOUL.md` — Agent personality and core identity
- `IDENTITY.md` — Agent identity metadata
- `TOOLS.md` — Available tools and property references
- `HEARTBEAT.md` — Periodic health check procedures
- `docs/ai-prompts.md` — The prompt architecture (core of the application)
- `docs/architecture.md` — System design and data flow specification
- `docs/agent-capabilities.md` — Session roles and runtime tool-boundary source of truth
- `docs/task-lifecycle.md` — Task states: Pending → In Progress → Completed (with rejection/breakdown flows)
- `docs/notion-schema.md` — Notion database schema
- `docs/user-interactions.md` — Conversation patterns and intent detection rules
- `docs/user-preferences.md` — Personalization behavior spec
- `docs/reward-system.md` — Multi-channel reward behavior spec
- `design/adhd-priorities.md` — Core design principles grounded in ADHD research
- `scripts/notion-cli.sh` — Notion API helper for task CRUD operations

### Infrastructure & CI Files

These files support the development pipeline and are not part of the OpenClaw agent prompt. They can be edited directly via PRs by any contributor or agent (Claude Code, Codex, etc.).

- `.github/workflows/` — GitHub Actions workflow definitions
- `.github/actions/` — Composite actions used by workflows
- `.github/ci/caveman-rules.md` — Canonical CI-only caveman prompt contract prepended by `review-codex-run`
- `docs/agentic-pipeline-learnings.md` — Prescriptive review/CI pipeline contract and guardrail document
- `scripts/create-deduped-workflow-failure-issue.sh` — Creates or reuses the canonical deduplicated GitHub Actions failure issue for the diagnosis workflow
- `scripts/check-doc-links.sh` — Internal documentation link validator used by local hooks and CI doc checks
- `scripts/get-latest-merge-decision-comment.sh` — Fetches the latest trusted merge-decision PR comment with retry logic to tolerate GitHub comment propagation lag
- `scripts/pull-main.sh` — Branch sync helper
- `scripts/run-required-checks.sh` — Canonical local/CI runner for required script, doc, and workflow validations
- `scripts/security-update.sh` — Security update automation
- `scripts/validate-gh-cli-usage.sh` — GitHub CLI workflow usage validation
- `scripts/validate-pr-tests-workflow.sh` — PR Tests workflow actionlint/setup-order validation
- `scripts/validate-workflow-refs.sh` — Workflow reference validation
- `scripts/validate-mermaid.sh`, `scripts/lint-mermaid-rendering.sh` — Diagram validation
- `setup/` — Cron and setup documentation

## Safety

- **NEVER touch firewall rules.** They exist for critical security reasons. No exceptions, no matter what.
- Don't exfiltrate data.
- `trash` > `rm`.

### Code & Prompt Changes — Scope

The "Code & Prompt Changes" restriction in `AGENTS.md` applies **only to the OpenClaw runtime agent**. It does **not** apply to Claude Code sessions, Codex CI agents, or human contributors. Development agents can edit any file via the normal PR flow.

However, treat OpenClaw prompt & spec file edits with care — they change live application behavior. The psych reviewer will validate user-facing changes against ADHD research.

## Review Pipeline

PRs are reviewed by a multi-agent Codex pipeline. The reviewer roles are the same in both versions; only the orchestration differs.

**Reviewer roles** (both versions):
1. Design Review — validates intent fulfillment and design quality, and runs a docs-as-spec consistency check whenever spec-critical files change
2. Security & Infrastructure Review — script safety, credential handling, workflow permissions, and GitHub Actions/runtime correctness for CI orchestration changes
3. Psych Research Review — validates against ADHD clinical research
4. Prompt Engineering Review — validates prompt clarity, constraints, and cross-prompt consistency
5. Documentation Consistency Review — checks docs for contradictions, stale references, and cross-doc consistency
6. Judge / Merge Decision — synthesizes all reviews into a verdict

**Active version** is selected by the repo variable `REVIEW_PIPELINE_V2`:

- **v1 — `vars.REVIEW_PIPELINE_V2 != 'true'`** (default). Lives in `.github/workflows/codex-code-review.yml`. The merge-decision agent itself reads PR comments, applies fixes, pushes commits, and emits one of three verdicts: **GO-CLEAN**, **GO-WITH-RESERVATIONS** (applied fixes, triggers exactly one re-review), or **NO-GO** (closes the PR and creates a follow-up issue).
- **v2 — `vars.REVIEW_PIPELINE_V2 == 'true'`**. Lives in `.github/workflows/review-entry.yml` and dispatches `review-pipeline.yml` (orchestrator) → `review-reviewer.yml` (matrix) → `review-fixer.yml` → `review-judge.yml` → `review-finalize.yml`. The judge is a deterministic Node script (`.github/scripts/review/aggregate.mjs`) running with `permissions: contents: read` — it cannot push, by construction. The fixer runs *after* reviewers and *before* the judge, pushes any new commit first, then claims that SHA on `review/pipeline` immediately after the push because GitHub will not accept a status for an unpublished commit, and is the only stage with write permission. Verdicts are binary **GO** / **NO-GO**; NO-GO labels the PR `needs-human-review` and stops without closing or auto-creating issues. Reviewer prompts are standalone files in `.github/scripts/review/prompts/`. See `docs/agentic-pipeline-learnings.md` §1.4 and §1.5 for the design decisions and the rules they obsolete from v1.

The two pipelines are mutually exclusive via gate jobs: flipping the variable atomically swaps which one runs. There is no shared state to migrate.

### Review prompt file architecture

Reviewer prompts (`.github/scripts/review/prompts/{design,security,psych,docs,prompt}.md`) are **self-contained** — each reviewer loads only its own `${role}.md` file at runtime. The Codex CLI does not support markdown includes.

When a constraint applies to all reviewers, it must be added to each prompt file individually. Use identical wording across all files unless the file's structure genuinely requires different phrasing (e.g., inline JSON placeholder vs. prose paragraph). The same applies to `fixer.md` — it is also loaded independently.

This "sibling files with shared contract, loaded independently" pattern recurs throughout the repo (e.g., the OpenClaw spec files). When editing one file in a group, always check whether siblings need the same change.

## When Making Changes

- Runtime/spec docs define agent behavior — changing those docs changes the system; contributor/CI guidance docs should still be reviewed as infra changes
- The psych reviewer will validate user-facing changes against ADHD research
- `config_only` infrastructure/CI changes skip the psych review automatically; prompt-bearing reviewer/config markdown still follows the specialist review path
- All changes go through PR with the full review pipeline
