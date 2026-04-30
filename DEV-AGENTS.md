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
- `HEARTBEAT.md` — Bootstrap stub; delegates to `docs/heartbeat-checks.md` (keeps per-turn context small)
- `docs/heartbeat-checks.md` — Authoritative heartbeat check list (stranded reminders, cron health, drift, Notion connectivity, dirty-pull recovery)
- `docs/ai-prompts/shared.md` — Base system prompt, intent dispatch, user preferences context, output/error/state handling (entry point for per-intent prompts)
- `docs/ai-prompts/intake.md` — Task Intake module (ADD_TASK): inference rules, sub-task generation, reminder detection
- `docs/ai-prompts/selection.md` — Task Selection module (GET_TASK): scoring weights, mood mapping
- `docs/ai-prompts/rejection.md` — Rejection Handling module (REJECT): shame-safe responses, escalation flow
- `docs/ai-prompts/cannot-finish.md` — Cannot Finish module (CANNOT_FINISH): progress gathering, sub-task creation
- `docs/ai-prompts/check-in.md` — Check-In Handling module (CHECK_IN): timing, shame-safe templates
- `docs/ai-prompts/breakdown.md` — Breakdown Assistance module (NEED_HELP): confidence detection, response levels
- `docs/architecture.md` — System design + data flow spec
- `docs/openclaw-integration.md` — OpenClaw runtime mapping, model routing, cron registration contract
- `docs/agent-capabilities.md` — Session roles + runtime tool-boundary source of truth
- `docs/task-lifecycle.md` — Task states: Pending → In Progress → Completed (with rejection/breakdown flows)
- `docs/notion-schema.md` — Notion database schema
- `docs/user-interactions.md` — Conversation patterns + intent detection rules
- `docs/user-preferences.md` — Personalization behavior spec
- `docs/reward-system.md` — Multi-channel reward behavior spec
- `design/adhd-priorities.md` — Core design principles grounded in ADHD research
- `scripts/notion-cli.sh` — Notion API helper for task CRUD
- `scripts/user-time-context.sh` — Timezone helper: resolves a reference timestamp to user-local date/time for reminder intake

### Infrastructure & CI Files

Support dev pipeline. Not OpenClaw prompt. Edit directly via PRs — any contributor or agent (Claude Code, Codex, etc.).

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
- `scripts/pull-main.sh` — Branch sync helper
- `scripts/run-required-checks.sh` — Canonical local/CI runner for required script, doc, workflow validations
- `scripts/security-update.sh` — Security update automation
- `scripts/validate-gh-cli-usage.sh` — GitHub CLI workflow usage validation
- `scripts/validate-pr-tests-workflow.sh` — PR Tests workflow actionlint/setup-order validation
- `scripts/validate-workflow-refs.sh` — Workflow reference validation
- `scripts/validate-mermaid.sh`, `scripts/lint-mermaid-rendering.sh` — Diagram validation
- `scripts/validate-model-refs.sh` — Enforces model tier consistency: every `litellm/<id>` resolves in template, `modelTiers` matches agent config, cron specs use cheap tier
- `scripts/validate-spec-catalog.sh` — Enforces that every `docs/*.md` spec file registered in the classifier's `is_spec_md()` is also listed in `docs/index.md` and this file's Key Files section
- `setup/` — Cron + setup docs

## Safety

- **NEVER touch firewall rules.** Critical security. No exceptions.
- Don't exfiltrate data.
- `trash` > `rm`.

### Code & Prompt Changes — Scope

"Code & Prompt Changes" restriction in `AGENTS.md` applies **only to OpenClaw runtime agent**. Not Claude Code, Codex CI, or human contributors. Dev agents edit any file via normal PR flow.

Treat OpenClaw prompt + spec file edits with care — change live app behavior. Psych reviewer validates user-facing changes against ADHD research.

### Prompt & Spec Files — Present Tense Only

The "OpenClaw Prompt & Spec Files" listed above are loaded into the runtime agent's session context every turn. They **are** the spec the agent operates from — not documentation of how the spec evolved. Write them like a system prompt, not a changelog.

Rules:

- **Present tense only.** Describe how the system behaves *right now*. No "now does X", "previously Y", "used to Z", "still [uses old approach]" (historical comparisons), "instead of being purely random", "previous architecture", "former bash daemons", "What changed:", "replaced old…", "before X shipped". Present-state uses of "still" ("while still running", "agents still control") are fine.
- **No `(Issue #N)` / `(PR #N)` / `(#N)` suffixes** on section headers, list items, or callouts. Issue and PR numbers go in the commit message, the PR body, the linked issue itself — not in the runtime prompt. They rot, and they pull the agent's attention onto historical scaffolding instead of the current rule.
- **Replace, don't diff.** When behavior changes, rewrite the section. Don't keep before/after framing or "Why we changed this:" notes in the spec — that belongs in the commit message.
- **No roadmaps in spec files.** Gantt charts, "future enhancements" lists, `:done` markers, and similar in-flight tracking belong in GitHub issues/projects or a clearly-labeled non-spec roadmap doc, not in runtime prompts.
- **Rationale belongs in present tense.** "Why this design:" framing is fine — explain the constraint that *currently* governs the choice. Avoid framing rationale as a post-mortem of an alternative that was tried and rejected.

This rule applies to every file in the "OpenClaw Prompt & Spec Files" list above (`AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `TOOLS.md`, `HEARTBEAT.md`, `docs/heartbeat-checks.md`, all `docs/ai-prompts/*.md`, `docs/architecture.md`, `docs/openclaw-integration.md`, `docs/agent-capabilities.md`, `docs/task-lifecycle.md`, `docs/notion-schema.md`, `docs/user-interactions.md`, `docs/user-preferences.md`, `docs/reward-system.md`, `design/adhd-priorities.md`).

It does **not** apply to `docs/agentic-pipeline-learnings.md` or other contributor/CI-only guidance, which legitimately carry historical context about how the dev pipeline got to its current shape.

## Review Pipeline

PRs reviewed by multi-agent review pipeline (Codex reviewers + fixer in v2). Roles same in both versions; orchestration differs.

**Reviewer roles**:
1. Design Review — validates intent + design quality; runs docs-as-spec consistency check on spec-critical changes
2. Security & Infrastructure Review — script safety, credential handling, workflow permissions, GH Actions/runtime correctness
3. Psych Research Review — validates against ADHD clinical research
4. Prompt Engineering Review — validates prompt clarity, constraints, cross-prompt consistency
5. Documentation Consistency Review — contradictions, stale refs, cross-doc consistency
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

"Sibling files with shared contract, loaded independently" pattern recurs throughout repo (e.g., OpenClaw spec files). Editing one file in group → check if siblings need same change.

## When Making Changes

- Runtime/spec docs define agent behavior — changing those docs changes system; contributor/CI guidance still reviewed as infra changes
- Psych reviewer validates user-facing changes against ADHD research
- `config_only` infra/CI changes skip psych review automatically; prompt-bearing reviewer/config markdown follows specialist review path
- All changes go through PR with full review pipeline