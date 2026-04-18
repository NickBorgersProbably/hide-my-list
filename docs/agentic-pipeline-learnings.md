# Agentic Pipeline Learnings

Forked from [home-automation](https://github.com/NickBorgers/home-automation), absorbed ~100 PRs of hard lessons. **Prescriptive**: each rule = current pipeline contract or guardrail future changes must preserve. Includes why + failure mode. PRs cited.

CI/review-pipeline guidance only. Not OpenClaw runtime spec. Canonical sources: `AGENTS.md`, `HEARTBEAT.md`, `docs/heartbeat-checks.md`, `setup/cron/*.md`, `docs/openclaw-integration.md`.

Two meta-lessons:

- **Silent failures = expensive.** Most fixes address things that ran but did wrong thing: dropped env vars, missing mounts, agents posting no comments, cron drifting silently. Add loud assertions + validation gates.
- **Pipeline = distributed system with humans in loop.** Loops, label state, re-trigger semantics must be explicit + capped. Default: don't re-run unless something explicitly asks.

---

## 1. Review Pipeline Architecture

### 1.1 Parallel review jobs are read-only; only dedicated single-writer stages push
**Why:** Parallel reviewers with write access pushed fixes that retriggered pipeline and posted near-duplicate comments. Fan-out review jobs stay read-only; branch mutations limited to dedicated single-writer stages. v1: `fix-test-failures` (pre-review) + `merge-decision` (post-review). v2: `review-fixer.yml` only; judge/finalize stays read-only.
**Before:** Every reviewer round produced 3× redundant comments.
**Evidence:** #70, #71, #274

### 1.2 Review progress state must live in durable GitHub state, not comments or workflow inputs
**Why:** Comment-pagination counters broke past page 1; `workflow_dispatch` inputs reset to 0 on manual reruns. v1: PR-scoped state on labels (`review-cycle-*`, `*-started`) — survive reruns, visible to humans. v2: execution state on SHA-scoped commit statuses (`review/pipeline`, `review/*`, `review/cycle`) — keyed to immutable commits. Both versions: comments + workflow inputs = observability only, not authority.
**Before:** Cycle counters silently reset, allowing infinite loops.
**Evidence:** #182, #234, #303, #320

### 1.3 Hard-cap review cycles in shell, with redundant job-level skips
**Why:** Shell script in `trigger-follow-up` independently counts cycle labels and refuses dispatch at cap; job-level `if:` conditions catch any escape. Defense in depth.
**Before:** Single PR looped 4+ times as agents contradicted each other's prior fixes.
**Evidence:** #303, #315, #301

### 1.4 Merge-decision verdict shape (v1: three-state; v2: binary)
**v1 (legacy `codex-code-review.yml`, active when `vars.REVIEW_PIPELINE_V2 != 'true'`):** Three states — GO-CLEAN / GO-WITH-RESERVATIONS / NO-GO. Binary GO/NO-GO with auto-retrigger on every push burned ~18 LLM runs per PR even for typo fixes; three states let merge agent collapse unnecessary loops: clean merges skip re-review, reservations get one re-review cycle, no-go closes PR with follow-up issue.
**v2 (new `review-entry.yml` graph, active when `vars.REVIEW_PIPELINE_V2 == 'true'`):** Two states — GO / NO-GO. Cost-control now structural: v2 fixer runs after reviewers + before judge; judge has `permissions: contents: read` and cannot push; fixer publishes new commit first then writes `review/pipeline` status immediately (GitHub rejects status for unpublished SHA). Re-review loops on finalized GO SHAs prevented by GO-only `All Required Agent Reviews = success` short-circuit at entry. NO-GO = human escalation: labels PR `needs-human-review`, posts one sticky comment, does **not** close PR or auto-create replacement issue (avoids lessons-learned-issue → new PR → NO-GO infinite loop). PR #343 introduces this; Phase 2/3 gate flip makes it active.
**Before:** Pipeline cost dominated by trivial PRs re-reviewed end-to-end (v1). v2 solves at orchestration layer instead of verdict layer.
**Evidence:** #315, #320, #322, #274 (v1); #336, #341, #342, #343 (v2)

### 1.5 Inline review comments are blocking inputs (v1: judge reads them; v2: reviewers fold them in)
**v1 (legacy `codex-code-review.yml`):** Merge-decision agent reads all inline PR comments via `gh api`, treats substantive change requests as blockers. Enforcement = "read every inline comment and apply judgment," not a resolution-state API check.
**v2:** Judge (`.github/scripts/review/aggregate.mjs`) = deterministic Node script, no Codex, no git credentials, no PR API access — consumes ONLY structured reviewer JSON artifacts conforming to `schema/reviewer-v1.json`. To preserve blocking property: each reviewer ingests inline PR comments via `gh api repos/.../pulls/{n}/comments` inside its own prompt and folds blocking change requests into `blocking_issues[]` with `source: "inline_comment"`. Schema's `source` enum encodes this contract. Authority chain inverts (reviewer-side instead of judge-side), user-visible invariant identical: inline change request still blocks PR.
**Before:** PRs auto-approved despite outstanding inline change requests (v1 root cause).
**Evidence:** #143 (v1); #341, #342 (v2 schema + judge contract)

### 1.6 Manual re-trigger is a `/review` comment, not close/reopen
**Why:** Close/reopen fired both `pull_request` trigger and `workflow_run` trigger; concurrency group cancelled one at random.
**Before:** Manual re-reviews silently no-op'd.
**Evidence:** #244, #234

### 1.7 Track immutable `reviewed_sha`, separate from branch ref
**Why:** Reviewers + fixers must check out immutable `reviewed_sha`, not mutable branch ref — branch may be deleted or advanced while pipeline works. Any branch-writing stage must re-read `origin/<head_ref>` immediately before push and refuse if tip no longer equals frozen `reviewed_sha`; otherwise silently overwrites newer author commits with changes prepared against stale code.
**Before:** Post-merge follow-ups failed with "could not fetch ref".
**Evidence:** #308, #351, #353, #354

**Manual regression playbook:** Open PR, let `review-entry.yml` freeze `reviewed_sha`, push another commit before `review-fixer.yml` reaches push step. Fixer should log that `origin/<head_ref>` moved, rewrite `fix-result.json` so `new_sha == input_sha == reviewed_sha`, add `skipped[]` reason, exit without pushing. Judge/finalize should evaluate unchanged reviewed tree instead of overwriting newer commit.

### 1.8 Dedupe workflow-failure issues by fingerprint
**Why:** Helper script fingerprints failures by (workflow, branch, commit) and reuses existing open issue.
**Before:** Every retry spawned a new failure issue, drowning issue tracker.
**Evidence:** #269

### 1.9 Guardrail: spec-critical `.md` files must stay on full review path
**Why:** Files like `setup/cron/reminder-check.md`, `TOOLS.md`, `SOUL.md` are *executable* — they define agent behavior. `docs_only=true` classifier = implementation shortcut, not semantic proof that matching Markdown is inert. Workflow correctly carves out known spec-critical Markdown like `design/adhd-priorities.md`, but future prompt-bearing docs under broad content trees like `design/` or `.github/` may be added without updating carve-outs. Treat as allowlist problem, not assume any Markdown-heavy directory is safe to bypass.
**Before:** Behavioral cron prompt slipped through with zero security review.
**Evidence:** #156, #142

### 1.10 Clear stale `*-started` labels before each new cycle
**Why:** Snapshot + clear `*-started` labels at cycle boundaries; only restore if new cycle label commits successfully.
**Before:** On cycle 2, humans couldn't tell which review stage was actually running.
**Evidence:** #320

### 1.11 Review-skip approvals must be SHA-bound, not PR-bound
**Why:** PR-level marker like `agent-reviews-passed` can outlive the diff it described. Safe auto-skip rule is version-specific but always SHA-bound: v1 = current head SHA must carry same-SHA `All Required Agent Reviews = success` status from a `GO-CLEAN` decision; v2 = orchestrator must see reviewed SHA already claimed on `review/pipeline`, with corresponding `review/*` and `review/cycle` statuses describing that exact commit chain, and any branch mutation still flows only through `review-fixer.yml`. PR labels communicate history to humans but must not gate execution for later commits.
**Before:** New head commits inherited green aggregate review check without any stage evaluating updated diff.
**Evidence:** #339, #338, #337

### 1.12 Security/infra review explicitly owns reviewer-routing regressions
**Why:** Review-pipeline dispatch + classifier changes can silently narrow who reviews future PRs while workflow still "works." When PR touches classifier, dispatch, or gating logic, Security & Infrastructure reviewer must compare proposed routing against current pipeline behavior and flag unintended loss of specialist coverage. Regressions that drop coverage for prompt/spec files, including `.github/scripts/review/prompts/*.md`, are blocking unless explicitly documented. v2: review-orchestration files must force security review even when PR is otherwise workflow-only or config-only.
**Before:** Reviewer prompt markdown under `.github/scripts/review/prompts/*.md` classified as config-only in v2, which would have skipped security, psych, and prompt review — no reviewer called it out.
**Evidence:** #343, #349

### 1.13 Stop on GO, keep retrying on NO-GO: asymmetric cycle control
**Why:** v2 fixer must push before claiming `review/pipeline` — GitHub's commit-status API rejects unpublished SHAs. Leaves bounded synchronize-event race: `review-entry-v2-<head_ref>` concurrency group serializes synchronize event behind still-running cycle; by time queued entry runs, post-fix SHA may already have moved from `review/pipeline = pending` to `success`. Without GO-only short-circuit, queued entry can re-review exact SHA cycle N already finalized. On drift-sensitive PRs (anything touching spec-critical Markdown), redundant cycle can legitimately find fresh cross-doc drift introduced by cycle N's fixer — see PR NickBorgersProbably/hide-my-list#397, which burned 3 full cycles with monotonically diminishing blocker counts (6 → 3 → 1).

Fix is split by verdict:

- **GO cycle is terminal.** `review-entry.yml` refuses dispatch when reviewed SHA already carries `All Required Agent Reviews = success` commit status (written by `review-finalize.yml:76-95` on GO only). GO means pipeline is done with this SHA; fresh cycle would only rediscover drift from prior fixer. `/review` comments bypass check (`issue_comment` events are event-gated out) so humans can force re-review manually.
- **NO-GO cycle retries.** `All Required Agent Reviews` status on NO-GO is `failure` — doesn't match GO-only short-circuit, so `synchronize` event from fixer's push continues into fresh cycle. Deliberate: NO-GO means "fixer could not address all blockers this round." Retry is bounded by `MAX` in `review-pipeline.yml` (currently `2`, i.e. up to one NO-GO retry). `cap-exhausted` on NO-GO correctly finalizes as NO-GO with `needs-human-review`; cycle 2 GO cleanly replaces cycle 1 NO-GO label.

Why **not** just lower `MAX` to `1`: `cap-exhausted` hard-codes `verdict='NO-GO'` (`review-pipeline.yml:131`). With `MAX=1`, every cycle-1 GO would be followed by synchronize event firing cycle 2, which caps immediately and stamps NO-GO on current HEAD — clobbering cycle 1 GO. Safely lowering `MAX` to 1 would require rewriting `cap-exhausted` to inherit parent SHA's verdict, which duplicates what GO short-circuit already does more cheaply at entry layer. `MAX=1` would also remove NO-GO retry capability.
**Before:** PR #397 posted three full Merge Decisions and ~15 reviewer comments for single docs-only PR already GO at cycle 1.
**Evidence:** #397

---

## 2. CI Runtime Infrastructure

### 2.1 Pre-create bind-mount source directories before workflows that mount them
**Why:** Docker silently fails when bind-mount source path doesn't exist. Any workflow mounting host config dirs into devcontainer should `mkdir -p` those paths first. Current Codex review pipeline does this for `~/.config/gh`, `~/.claude`, `~/.codex`; future workflows adding same mounts need same guard.
**Evidence:** #77, #78

### 2.2 Don't make CI reviewer containers depend on forwarded Anthropic/OAuth credentials
**Why:** Stable pattern = run review jobs through baked container config, pass only minimal runtime env needed. Today's Codex reviewer jobs forward `OPENAI_API_KEY=fake-key` and `GH_TOKEN=${WORKFLOW_PAT}`; they do not rely on `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` passthrough.
**Before:** Earlier iterations tried to forward LLM auth env vars directly, failed in confusing ways when container runtime forwarding differed from expectations.
**Evidence:** #90, #100

### 2.3 Use `WORKFLOW_PAT` as `GH_TOKEN` for `gh` inside devcontainers
**Why:** `github.token` doesn't authenticate `gh pr comment` from inside `devcontainers/ci@v0.3` container on self-hosted runners.
**Before:** Reviewers ran successfully and posted zero comments. Perfect silent failure.
**Evidence:** #91 (applied across nine occurrences)

### 2.4 External fork PRs do not run Codex review; any devcontainer build must use the `main` ref
**Why:** Untrusted PR code on homelab runners with credential access = privilege escalation. Current workflow blocks Codex review for non-collaborator PR authors; devcontainer build path checks out `main` rather than PR ref to avoid Dockerfile injection.
**Evidence:** #110

### 2.5 Treat BuildKit default attestations as an explicit compatibility choice
**Why:** BuildKit's default attestations can produce OCI manifest lists without valid platform metadata, breaking `docker push`. If current image-push path needs `BUILDX_NO_DEFAULT_ATTESTATIONS=1`, set it explicitly in workflow code rather than docs only.
**Evidence:** #133

### 2.6 Use the custom `run-devcontainer` action for run steps; reserve `devcontainers/ci@v0.3` for build-and-push only
**Why:** `devcontainers/ci@v0.3` hit post-step cleanup crash on self-hosted runners. Repo avoids this by building/pushing with `devcontainers/ci@v0.3`, then running commands through `.github/actions/run-devcontainer/action.yml`, which uses `@devcontainers/cli up/exec` directly.
**Evidence:** #179

### 2.7 Bake the Codex CLI into the Dockerfile; route models via LiteLLM with explicit `CODEX_MODEL`
**Why:** Runtime CLI downloads stalled on slow networks; missing model defaults caused fallback to mismatched models. Downgrading three of six review stages to `gpt-5-mini` cut review cost ~45% with no measurable quality loss.
**Evidence:** #110, #130, #145, #292

### 2.8 Keep local-only SSH agent mounts out of CI
**Why:** CI runners have no SSH agent, so any `${localEnv:SSH_AUTH_SOCK}` bind mount becomes empty or invalid source. Current repo avoids this by not defining that mount in `.devcontainer/devcontainer.json`; if future devcontainer adds it back for local use, CI override generator must strip or override it explicitly.
**Before:** Seven iterative PRs across two days chasing variations of "invalid value for 'source'".
**Evidence:** #224, #226, #230, #248, #260, #270, #280

### 2.9 Validate workflows with `actionlint` + cross-file ref checks, and keep a devcontainer smoke test for container changes
**Why:** Yamllint passes broken `uses:` refs, missing script paths, and local-action checkout races. Current `pr-tests.yml` always runs canonical local/CI runner in `scripts/run-required-checks.sh`, which includes `actionlint` plus `validate-workflow-refs.sh` for workflow changes, plus devcontainer build smoke test when `.devcontainer/**` changes. If workflow changes start depending on new devcontainer behavior, extend smoke-test trigger instead of assuming yamllint is enough.
**Evidence:** #232, #207, #219

### 2.10 Scope env loading per script; fall back to `.env` for host `gh` auth
**Why:** Each runtime script imports only its own env vars via `scripts/load-env.sh`. Host-based maintenance scripts validate `gh` auth via `.env` fallback so cron jobs don't silently fail when user's interactive session isn't logged in.
**Evidence:** #271, #284

---

## 3. OpenClaw Runtime, Cron, and Recovery

### 3.1 Durable cron jobs replace bash daemons
**Why:** Signal-based bash daemons died silently with no monitoring. OpenClaw's durable cron has built-in retry, observability, and ownership.
**Before:** Reminders unreliable; pipeline monitoring inconsistent.
**Evidence:** #112, #199, #263

### 3.2 Heartbeat enforces cron spec drift, not just job existence
**Why:** Heartbeat compares live cron jobs against canonical specs in `setup/cron/` and patches drift via `CronUpdate`. Cron spec re-application after `pull-main` happens on heartbeat's next cycle — isolated `pull-main` session cannot reliably call `CronList`/`CronUpdate`.
**Before:** Cron jobs drifted from registered config, causing exponential backoff and silent failures.
**Evidence:** #238, #266, #268, #277

### 3.3 Dirty-pull recovery preserves work via `.pull-dirty` + GitHub issue
**Why:** When `git pull` fails on uncommitted changes or merge conflict, write structured `.pull-dirty` signal and open GitHub issue capturing changes *before* resetting.
**Before:** Local agent modifications silently lost on conflicts.
**Evidence:** #146

### 3.4 The "OpenClaw can't edit prompts" rule is OpenClaw-only
**Why:** AGENTS.md restricts runtime agent from editing repo-managed prompt/spec files. Does **not** apply to Claude Code, Codex, or human contributors — those go through normal PR flow.
**Before:** Claude Code refused legitimate infrastructure edits, citing AGENTS.md as if universal.
**Evidence:** #247

### 3.5 Prefer passing stable IDs through Actions; keep body handoff narrowly scoped
**Why:** Multi-line bodies in Actions outputs cause shell-escaping failures and context loss. Default pattern: pass only stable identifiers like `COMMENT_ID`, `ISSUE_NUMBER`, `RUN_ID`, then `gh api` full body inside devcontainer. Narrow exception: reviewer/fixer jobs may receive base64-encoded PR title/body for immediate author-intent evaluation without extra API round-trip. Keep that exception tightly scoped.
**Evidence:** #41, #178

### 3.6 Reopen-gate PRs until `workflow_run` reviews complete
**Why:** Closing + reopening previously bypassed review gate — `all-reviews-passed` check evaluated before reviews actually re-ran.
**Evidence:** #92, #99

### 3.7 Cron specs must declare the full isolated-session contract explicitly
**Why:** Missing or inconsistent registration fields caused accumulating errors and ambiguous routing. Canonical cron specs spell out `name`, `durable`, `schedule`, `prompt`, `sessionTarget`, `model`, `payload.kind`, `timeout-seconds`, and stay silent by omitting direct-delivery `to`.
**Evidence:** #199, #231, #268

### 3.8 MEMORY.md is for lessons; tasks belong in Notion
**Why:** Mixing to-dos into persistent memory caused drift between "what I should do" and "what I learned." Hard separation: Notion for actions, MEMORY.md for context.
**Evidence:** Reinforced across #112 and AGENTS.md itself.

---

## How to use this doc

Touching pipeline:
1. Find section matching your change.
2. If change *reverses* a rule, burden of proof is on you — re-read cited PRs first.
3. New failure mode discovered? Add numbered rule here in same prescriptive format.