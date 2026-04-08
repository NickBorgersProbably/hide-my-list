# Agentic Pipeline Learnings

This repo's agentic review/CI pipeline was forked from [home-automation](https://github.com/NickBorgers/home-automation) and has since absorbed roughly 100 PRs of hard lessons. This document is **prescriptive**: each rule is either part of the current pipeline contract or a guardrail that future pipeline changes must preserve. Each entry includes a one-line explanation of *why* and a brief note on the failure mode that taught us. PR numbers are cited so you can dig in.

This file is CI/review-pipeline guidance for contributors and reviewers. It is **not** part of the OpenClaw runtime prompt/behavior spec. When sections below reference runtime, cron, or recovery behavior, the canonical source remains the owning docs such as `AGENTS.md`, `HEARTBEAT.md`, `setup/cron/*.md`, and `docs/openclaw-integration.md`.

Two meta-lessons span everything below:

- **Silent failures are the expensive ones.** Most fixes here address things that ran successfully but did the wrong thing: dropped env vars, missing mounts, agents that posted no comments, cron jobs that drifted with no alert. Add loud assertions and validation gates; trust nothing silent.
- **The pipeline is a distributed system with humans in the loop.** Loops, label state, and re-trigger semantics need to be explicit and capped. Default to *don't re-run* unless something explicitly asks for it.

---

## 1. Review Pipeline Architecture

### 1.1 Parallel review jobs are read-only; only dedicated single-writer stages push
**Why:** When parallel reviewers had write access they pushed fixes that retriggered the whole pipeline and posted near-duplicate comments. Both pipeline versions keep the fan-out review jobs read-only; branch mutations are limited to dedicated single-writer stages. In v1, those are jobs such as `fix-test-failures` (pre-review) and `merge-decision` (post-review). In v2, `review-fixer.yml` is the only branch-writing stage and the judge/finalize path stays read-only.
**Before:** Every reviewer round produced 3× redundant comments.
**Evidence:** #70, #71, #274

### 1.2 Review progress state must live in durable GitHub state, not comments or workflow inputs
**Why:** Comment-pagination based counters broke past page 1, and `workflow_dispatch` inputs reset to 0 on manual reruns. The durable state carrier depends on pipeline version: v1 stores PR-scoped cycle/progress state on labels (`review-cycle-*`, `*-started`) because labels survive reruns and stay visible to humans; v2 stores execution state on SHA-scoped commit statuses (`review/pipeline`, `review/*`, `review/cycle`) because the orchestrator/fixer/judge/finalize graph is keyed to immutable commits rather than mutable PR labels. In both versions, comments and workflow inputs are observability aids, not authority.
**Before:** Cycle counters silently reset, allowing infinite loops.
**Evidence:** #182, #234, #303, #320

### 1.3 Hard-cap review cycles in shell, with redundant job-level skips
**Why:** A shell script in `trigger-follow-up` independently counts cycle labels and refuses dispatch at the cap; job-level `if:` conditions catch any escape. Defense in depth.
**Before:** A single PR looped 4+ times as agents contradicted each other's prior fixes.
**Evidence:** #303, #315, #301

### 1.4 Merge-decision verdict shape (v1: three-state; v2: binary)
**v1 (legacy `codex-code-review.yml`, active when `vars.REVIEW_PIPELINE_V2 != 'true'`):** Three states — GO-CLEAN / GO-WITH-RESERVATIONS / NO-GO. Binary GO/NO-GO with auto-retrigger on every push had burned ~18 LLM runs per PR even for typo fixes; three states let the merge agent collapse unnecessary loops: clean merges skip re-review, reservations get exactly one re-review cycle, no-go closes the PR with a follow-up issue.
**v2 (new `review-entry.yml` graph, active when `vars.REVIEW_PIPELINE_V2 == 'true'`):** Two states — GO / NO-GO. The cost-control mechanism v1 needed three states for is now structural: the v2 fixer runs *after* reviewers and *before* the judge, the judge has `permissions: contents: read` and cannot push, and the fixer claims its output SHA on `review/pipeline` *before* publishing the push so the synchronize event hits already-claimed dedup and exits. Re-review-loops-from-autofix are impossible by construction, so the third state isn't needed. NO-GO in v2 is the human-escalation path: it labels the PR `needs-human-review`, posts one sticky comment, and **does not** close the PR or auto-create a replacement issue (avoids the lessons-learned-issue → new PR → NO-GO infinite loop class). PR #343 introduces this; PR with the gate flip (Phase 2/3) makes it active.
**Before:** Pipeline cost was dominated by trivial PRs being re-reviewed end-to-end (v1 problem). v2 solves the same problem at the orchestration layer instead of at the verdict layer.
**Evidence:** #315, #320, #322, #274 (v1); #336, #341, #342, #343 (v2)

### 1.5 Inline review comments are blocking inputs (v1: judge reads them; v2: reviewers fold them in)
**v1 (legacy `codex-code-review.yml`):** The merge-decision agent reads all inline PR comments via `gh api` and treats substantive change requests there as blockers, not just review summaries. The enforcement mechanism is "read every inline comment and apply judgment," not a separate resolution-state API check.
**v2 (new pipeline):** The judge (`.github/scripts/review/aggregate.mjs`) is a deterministic Node script with no Codex, no git credentials, and no PR API access — it consumes ONLY structured reviewer JSON artifacts conforming to `schema/reviewer-v1.json`. To preserve the same property as v1 (inline comments are blockers), each reviewer is responsible for ingesting inline PR comments via `gh api repos/.../pulls/{n}/comments` inside its own prompt and folding any blocking change requests into its `blocking_issues[]` array with `source: "inline_comment"`. The schema's `source` enum encodes this contract. The authority chain inverts (reviewer-side ingestion instead of judge-side), but the user-visible invariant is identical: an inline change request still blocks the PR.
**Before:** PRs were getting auto-approved despite outstanding inline change requests (v1 root cause).
**Evidence:** #143 (v1); #341, #342 (v2 schema + judge contract)

### 1.6 Manual re-trigger is a `/review` comment, not close/reopen
**Why:** Close/reopen fired both a direct `pull_request` trigger and a `workflow_run` trigger; the concurrency group cancelled one of them at random.
**Before:** Manual re-reviews silently no-op'd.
**Evidence:** #244, #234

### 1.7 Track immutable `reviewed_sha`, separate from the branch ref
**Why:** Once a PR merges, the branch may be deleted. Follow-up validation must check out the SHA we reviewed, not the (now missing) branch.
**Before:** Post-merge follow-ups failed with "could not fetch ref".
**Evidence:** #308

### 1.8 Dedupe workflow-failure issues by fingerprint
**Why:** A helper script fingerprints failures by (workflow, branch, commit) and reuses any existing open issue.
**Before:** Every retry spawned a new failure issue, drowning the issue tracker.
**Evidence:** #269

### 1.9 Guardrail: spec-critical `.md` files should stay on the full review path
**Why:** Files like `setup/cron/reminder-check.md`, `TOOLS.md`, and `SOUL.md` are *executable* — they define agent behavior. The `docs_only=true` classifier is only an implementation shortcut, not a semantic proof that every matching Markdown file is inert. The workflow now correctly carves out known spec-critical Markdown such as `design/adhd-priorities.md`, but the remaining risk is future prompt-bearing docs under broad content trees like `design/` or `.github/` being added without updating the carve-outs. Classifier tightening should keep treating these paths as an allowlist problem, not assume an entire Markdown-heavy directory is safe to bypass the full review path.
**Before:** A behavioral cron prompt slipped through with zero security review.
**Evidence:** #156, #142

### 1.10 Clear stale `*-started` labels before each new cycle
**Why:** Snapshot and clear `*-started` labels at cycle boundaries; only restore if the new cycle label commits successfully.
**Before:** On cycle 2, humans couldn't tell which review stage was actually running.
**Evidence:** #320

### 1.11 Review-skip approvals must be SHA-bound, not PR-bound
**Why:** A PR-level marker such as `agent-reviews-passed` can outlive the diff it originally described. The safe auto-skip rule is version-specific but always SHA-bound: in v1, the current head SHA must already carry the same-SHA `All Required Agent Reviews = success` status from a `GO-CLEAN` merge decision; in v2, the orchestrator must see the reviewed SHA already claimed on `review/pipeline`, with the corresponding `review/*` and `review/cycle` statuses describing that exact commit chain, and any branch mutation must still flow only through `review-fixer.yml` as the sole writer. PR labels can still communicate history to humans, but they must not gate execution for later commits.
**Before:** New head commits inherited a green aggregate review check without any stage evaluating the updated diff.
**Evidence:** #339, #338, #337

### 1.12 Security/infra review explicitly owns reviewer-routing regressions
**Why:** Review-pipeline dispatch and classifier changes can silently narrow who reviews future PRs while the workflow still "works." When a PR touches classifier, dispatch, or gating logic, the Security & Infrastructure reviewer must compare the proposed routing against the current pipeline behavior and flag any unintended loss of specialist coverage. Regressions that drop coverage for prompt/spec files, including `.github/scripts/review/prompts/*.md`, are blocking unless the PR explicitly documents and justifies the change.
**Before:** Reviewer prompt markdown under `.github/scripts/review/prompts/*.md` was classified as config-only in v2, which would have skipped security, psych, and prompt review and no reviewer called it out.
**Evidence:** #343, #349

---

## 2. CI Runtime Infrastructure

### 2.1 Pre-create bind-mount source directories before workflows that mount them
**Why:** Docker silently fails (or errors only at startup) when a bind-mount source path doesn't exist. Any workflow that mounts host config dirs into a devcontainer should `mkdir -p` those paths first. The current Codex review pipeline does this for `~/.config/gh`, `~/.claude`, and `~/.codex`; future workflows that add the same mounts need the same guard.
**Evidence:** #77, #78

### 2.2 Don't make CI reviewer containers depend on forwarded Anthropic/OAuth credentials
**Why:** The stable pattern in this repo is to run review jobs through baked container config and pass only the minimal runtime env they actually need. Today's Codex reviewer jobs forward `OPENAI_API_KEY=fake-key` and `GH_TOKEN=${WORKFLOW_PAT}` into the container; they do not rely on `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` passthrough.
**Before:** Earlier iterations tried to forward LLM auth env vars directly and failed in confusing ways when container runtime forwarding differed from expectations.
**Evidence:** #90, #100

### 2.3 Use `WORKFLOW_PAT` as `GH_TOKEN` for `gh` inside devcontainers
**Why:** `github.token` doesn't authenticate `gh pr comment` from inside a `devcontainers/ci@v0.3` container on self-hosted runners.
**Before:** Reviewers ran successfully and posted zero comments. Perfect silent failure.
**Evidence:** #91 (applied across nine occurrences)

### 2.4 External fork PRs do not run Codex review; any devcontainer build must use the `main` ref
**Why:** Untrusted PR code on homelab runners with credential access is a privilege escalation. The current workflow blocks Codex review for non-collaborator PR authors, and the devcontainer build path checks out `main` rather than the PR ref to avoid Dockerfile injection.
**Evidence:** #110

### 2.5 Treat BuildKit default attestations as an explicit compatibility choice
**Why:** BuildKit's default attestations can produce OCI manifest lists without valid platform metadata, breaking `docker push`. If the current image-push path needs `BUILDX_NO_DEFAULT_ATTESTATIONS=1`, that knob should be set explicitly in workflow code rather than living only in docs.
**Evidence:** #133

### 2.6 Use the custom `run-devcontainer` action for run steps; reserve `devcontainers/ci@v0.3` for build-and-push only
**Why:** `devcontainers/ci@v0.3` hit a post-step cleanup crash on self-hosted runners. The repo avoids that by building/pushing with `devcontainers/ci@v0.3`, then running commands through `.github/actions/run-devcontainer/action.yml`, which uses `@devcontainers/cli up/exec` directly.
**Evidence:** #179

### 2.7 Bake the Codex CLI into the Dockerfile; route models via LiteLLM with explicit `CODEX_MODEL`
**Why:** Runtime CLI downloads stalled on slow networks; missing model defaults caused fallback to mismatched models. Downgrading three of six review stages to `gpt-5-mini` cut review cost ~45% with no measurable quality loss.
**Evidence:** #110, #130, #145, #292

### 2.8 Keep local-only SSH agent mounts out of CI
**Why:** CI runners have no SSH agent, so any `${localEnv:SSH_AUTH_SOCK}` bind mount becomes an empty or invalid source. The current repo avoids the problem by not defining that mount in `.devcontainer/devcontainer.json`; if a future devcontainer adds it back for local use, the CI override generator must strip or override it explicitly.
**Before:** Seven iterative PRs across two days chasing variations of "invalid value for 'source'".
**Evidence:** #224, #226, #230, #248, #260, #270, #280

### 2.9 Validate workflows with `actionlint` + cross-file ref checks, and keep a devcontainer smoke test for container changes
**Why:** Yamllint passes broken `uses:` refs, missing script paths, and local-action checkout races. The current `pr-tests.yml` path always runs `actionlint` plus `validate-workflow-refs.sh` for workflow changes, and it adds a devcontainer build smoke test when `.devcontainer/**` changes. If workflow changes start depending on new devcontainer behavior, extend that smoke-test trigger instead of assuming yamllint is enough.
**Evidence:** #232, #207, #219

### 2.10 Scope env loading per script; fall back to `.env` for host `gh` auth
**Why:** Each runtime script imports only its own env vars via `scripts/load-env.sh`. Host-based maintenance scripts validate `gh` auth via a `.env` fallback so cron jobs don't silently fail when the user's interactive session isn't logged in.
**Evidence:** #271, #284

---

## 3. OpenClaw Runtime, Cron, and Recovery

### 3.1 Durable cron jobs replace bash daemons
**Why:** Signal-based bash daemons died silently with no monitoring. OpenClaw's durable cron has built-in retry, observability, and ownership.
**Before:** Reminders were unreliable and pipeline monitoring inconsistent.
**Evidence:** #112, #199, #263

### 3.2 Heartbeat enforces cron spec drift, not just job existence
**Why:** Heartbeat compares live cron jobs against canonical specs in `setup/cron/` and patches drift via `CronUpdate`. Cron spec re-application after a `pull-main` run happens on heartbeat's next cycle, because the isolated `pull-main` session cannot reliably call `CronList`/`CronUpdate`.
**Before:** Cron jobs drifted from registered config, causing exponential backoff and silent failures.
**Evidence:** #238, #266, #268, #277

### 3.3 Dirty-pull recovery preserves work via `.pull-dirty` + GitHub issue
**Why:** When `git pull` fails on uncommitted changes or merge conflict, write a structured `.pull-dirty` signal and open a GitHub issue capturing the changes *before* resetting.
**Before:** Local agent modifications were silently lost on conflicts.
**Evidence:** #146

### 3.4 The "OpenClaw can't edit prompts" rule is OpenClaw-only
**Why:** AGENTS.md restricts the runtime agent from editing repo-managed prompt/spec files. That restriction does **not** apply to Claude Code, Codex, or human contributors — they go through normal PR flow.
**Before:** Claude Code refused legitimate infrastructure edits, citing AGENTS.md as if it were universal.
**Evidence:** #247

### 3.5 Prefer passing event IDs through Actions; fetch large bodies inside the container
**Why:** Multi-line comment bodies in Actions outputs cause shell-escaping failures and context loss. The clean pattern is to pass only `COMMENT_ID`, `ISSUE_NUMBER`, `RUN_ID`, then `gh api` the body inside the devcontainer. Some current review jobs still pass base64-encoded PR/issue bodies; future refactors should converge on ID-only handoff instead of expanding that exception.
**Evidence:** #41, #178

### 3.6 Reopen-gate PRs until `workflow_run` reviews complete
**Why:** Closing and reopening a PR previously bypassed the review gate because the `all-reviews-passed` check evaluated before reviews actually re-ran.
**Evidence:** #92, #99

### 3.7 Cron specs must declare the full isolated-session contract explicitly
**Why:** Missing or inconsistent registration fields caused accumulating errors and ambiguous routing. In the current design, canonical cron specs spell out `name`, `durable`, `schedule`, `prompt`, `sessionTarget`, `model`, `payload.kind`, and `timeout-seconds`, and they stay silent by omitting any direct-delivery `to`.
**Evidence:** #199, #231, #268

### 3.8 MEMORY.md is for lessons; tasks belong in Notion
**Why:** Mixing to-dos into persistent memory caused drift between "what I should do" and "what I learned". Hard separation: Notion for actions, MEMORY.md for context.
**Evidence:** Reinforced across #112 and AGENTS.md itself.

---

## How to use this doc

If you're touching the pipeline:
1. Find the section that matches your change.
2. If your change *reverses* one of these rules, the burden of proof is on you — re-read the cited PRs first.
3. If you discover a new failure mode the hard way, add a numbered rule here in the same prescriptive format.
