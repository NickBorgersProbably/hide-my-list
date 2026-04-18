# DEV-AGENTS.md — hide-my-list

Dev agent context for Claude Code, Codex, human contributors. OpenClaw runtime instructions in `AGENTS.md` — edits there change app behavior.

## Architecture

- **Runtime**: OpenClaw agent (no standalone server)
- **Storage**: Notion database via API
- **Scripts**: `scripts/` — Notion CLI helpers + infra tooling
- **Docs**: `docs/` — runtime behavior specs, contributor/CI guidance where noted
- **Design**: `design/` — ADHD-informed design priorities
- **OpenClaw integration**: See `docs/openclaw-integration.md`

## Key Files

### OpenClaw Prompt & Spec Files

Define OpenClaw agent behavior — *are* the application. Change one = change agent.

- `AGENTS.md` — OpenClaw runtime agent instructions (bootstrap, auto-loaded)
- `SOUL.md` — Agent personality + core identity
- `IDENTITY.md` — Agent identity metadata
- `TOOLS.md` — Available tools + property references
- `HEARTBEAT.md` — Periodic health check procedures
- `docs/ai-prompts.md` — Prompt architecture (core of app)
- `docs/architecture.md` — System design + data flow spec
- `docs/agent-capabilities.md` — Session roles + runtime tool-boundary source of truth
- `docs/task-lifecycle.md` — Task states: Pending → In Progress → Completed (with rejection/breakdown flows)
- `docs/notion-schema.md` — Notion database schema
- `docs/user-interactions.md` — Conversation patterns + intent detection rules
- `docs/user-preferences.md` — Personalization behavior spec
- `docs/reward-system.md` — Multi-channel reward behavior spec
- `design/adhd-priorities.md` — Core design principles grounded in ADHD research
- `scripts/notion-cli.sh` — Notion API helper for task CRUD

### Infrastructure & CI Files

Support dev pipeline. Not OpenClaw prompt. Edit directly via PRs — any contributor or agent (Claude Code, Codex, etc.).

- `.github/workflows/` — GitHub Actions workflow definitions
- `.github/actions/` — Composite actions used by workflows
- `.github/ci/caveman-rules.md` — Canonical CI-only caveman prompt contract prepended by `review-codex-run`
- `docs/agentic-pipeline-learnings.md` — Prescriptive review/CI pipeline contract + guardrail doc
- `scripts/create-deduped-workflow-failure-issue.sh` — Creates/reuses canonical deduplicated GH Actions failure issue for diagnosis workflow
- `scripts/check-doc-links.sh` — Internal doc link validator for local hooks + CI doc checks
- `scripts/get-latest-merge-decision-comment.sh` — Fetches latest trusted merge-decision PR comment with retry for GitHub comment propagation lag
- `scripts/pull-main.sh` — Branch sync helper
- `scripts/run-required-checks.sh` — Canonical local/CI runner for required script, doc, workflow validations
- `scripts/security-update.sh` — Security update automation
- `scripts/validate-gh-cli-usage.sh` — GitHub CLI workflow usage validation
- `scripts/validate-pr-tests-workflow.sh` — PR Tests workflow actionlint/setup-order validation
- `scripts/validate-workflow-refs.sh` — Workflow reference validation
- `scripts/validate-mermaid.sh`, `scripts/lint-mermaid-rendering.sh` — Diagram validation
- `setup/` — Cron + setup docs

## Safety

- **NEVER touch firewall rules.** Critical security. No exceptions.
- Don't exfiltrate data.
- `trash` > `rm`.

### Code & Prompt Changes — Scope

"Code & Prompt Changes" restriction in `AGENTS.md` applies **only to OpenClaw runtime agent**. Not Claude Code, Codex CI, or human contributors. Dev agents edit any file via normal PR flow.

Treat OpenClaw prompt + spec file edits with care — change live app behavior. Psych reviewer validates user-facing changes against ADHD research.

## Review Pipeline

PRs reviewed by multi-agent Codex pipeline. Roles same in both versions; orchestration differs.

**Reviewer roles** (both versions):
1. Design Review — validates intent + design quality; runs docs-as-spec consistency check on spec-critical changes
2. Security & Infrastructure Review — script safety, credential handling, workflow permissions, GH Actions/runtime correctness
3. Psych Research Review — validates against ADHD clinical research
4. Prompt Engineering Review — validates prompt clarity, constraints, cross-prompt consistency
5. Documentation Consistency Review — contradictions, stale refs, cross-doc consistency
6. Judge / Merge Decision — synthesizes all reviews into verdict

**Active version** selected by repo variable `REVIEW_PIPELINE_V2`:

- **v1 — `vars.REVIEW_PIPELINE_V2 != 'true'`** (default). Lives in `.github/workflows/codex-code-review.yml`. Merge-decision agent reads PR comments, applies fixes, pushes commits, emits one of three verdicts: **GO-CLEAN**, **GO-WITH-RESERVATIONS** (fixes applied, triggers exactly one re-review), **NO-GO** (closes PR + creates follow-up issue).
- **v2 — `vars.REVIEW_PIPELINE_V2 == 'true'`**. Lives in `.github/workflows/review-entry.yml`, dispatches `review-pipeline.yml` (orchestrator) → `review-reviewer.yml` (matrix) → `review-fixer.yml` → `review-judge.yml` → `review-finalize.yml`. Judge = deterministic Node script (`.github/scripts/review/aggregate.mjs`) with `permissions: contents: read` — cannot push by construction. Fixer runs after reviewers, before judge; pushes new commit first, then claims that SHA on `review/pipeline` immediately after push (GitHub rejects status for unpublished commit); only stage with write permission. Verdicts binary **GO** / **NO-GO**; NO-GO labels PR `needs-human-review`, stops without closing or auto-creating issues. Reviewer prompts = standalone files in `.github/scripts/review/prompts/`. See `docs/agentic-pipeline-learnings.md` §1.4 + §1.5 for design decisions + obsoleted v1 rules.

Two pipelines mutually exclusive via gate jobs: flip variable = atomically swap which runs. No shared state to migrate.

### Review prompt file architecture

Reviewer prompts (`.github/scripts/review/prompts/{design,security,psych,docs,prompt}.md`) **self-contained** — each reviewer loads only its own `${role}.md` at runtime. Codex CLI doesn't support markdown includes.

Constraint applies to all reviewers → add to each prompt file individually. Use identical wording across files unless structure requires different phrasing (e.g., inline JSON placeholder vs. prose). Same applies to `fixer.md` — loaded independently.

"Sibling files with shared contract, loaded independently" pattern recurs throughout repo (e.g., OpenClaw spec files). Editing one file in group → check if siblings need same change.

## When Making Changes

- Runtime/spec docs define agent behavior — changing those docs changes system; contributor/CI guidance still reviewed as infra changes
- Psych reviewer validates user-facing changes against ADHD research
- `config_only` infra/CI changes skip psych review automatically; prompt-bearing reviewer/config markdown follows specialist review path
- All changes go through PR with full review pipeline