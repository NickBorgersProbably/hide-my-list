# Agentic Pipeline Learnings

This repo's agentic review/CI pipeline was forked from [home-automation](https://github.com/NickBorgers/home-automation) and has since absorbed roughly 100 PRs of hard lessons. This document is **prescriptive**: each rule is what we now do, with a one-line explanation of *why* and a brief note on the failure mode that taught us. PR numbers are cited so you can dig in.

Two meta-lessons span everything below:

- **Silent failures are the expensive ones.** Most fixes here address things that ran successfully but did the wrong thing: dropped env vars, missing mounts, agents that posted no comments, cron jobs that drifted with no alert. Add loud assertions and validation gates; trust nothing silent.
- **The pipeline is a distributed system with humans in the loop.** Loops, label state, and re-trigger semantics need to be explicit and capped. Default to *don't re-run* unless something explicitly asks for it.

---

## 1. Review Pipeline Architecture

### 1.1 Reviewers are read-only; only `merge-decision` pushes
**Why:** When parallel reviewers had write access they pushed fixes that retriggered the whole pipeline and posted near-duplicate comments. One pusher = one source of truth.
**Before:** Every reviewer round produced 3× redundant comments.
**Evidence:** #70, #71, #274

### 1.2 State lives on PR labels, not comments or workflow inputs
**Why:** Comment-pagination based counters broke past page 1, and `workflow_dispatch` inputs reset to 0 on manual reruns. Labels are durable across reruns and visible to humans.
**Before:** Cycle counters silently reset, allowing infinite loops.
**Evidence:** #182, #234, #303, #320

### 1.3 Hard-cap review cycles in shell, with redundant job-level skips
**Why:** A shell script in `trigger-follow-up` independently counts cycle labels and refuses dispatch at the cap; job-level `if:` conditions catch any escape. Defense in depth.
**Before:** A single PR looped 4+ times as agents contradicted each other's prior fixes.
**Evidence:** #303, #315, #301

### 1.4 Three-state merge decisions: GO-CLEAN / GO-WITH-RESERVATIONS / NO-GO
**Why:** Binary GO/NO-GO with auto-retrigger on every push burned ~18 LLM runs per PR even for typo fixes. Three states let the merge agent collapse unnecessary loops: clean merges skip re-review, reservations get exactly one re-review cycle, no-go closes the PR with a follow-up issue.
**Before:** Pipeline cost was dominated by trivial PRs being re-reviewed end-to-end.
**Evidence:** #315, #320, #322, #274

### 1.5 Inline review comments are blocking
**Why:** The merge-decision agent reads all inline PR comments via `gh api` and treats unresolved ones as blockers, not just review summaries.
**Before:** PRs were getting auto-approved despite outstanding inline change requests.
**Evidence:** #143

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

### 1.9 Spec-critical `.md` files always trigger security review
**Why:** Files like `setup/cron/reminder-check.md`, `TOOLS.md`, and `SOUL.md` are *executable* — they define agent behavior. The `docs_only` skip applies only to inert research docs and `README.md`.
**Before:** A behavioral cron prompt slipped through with zero security review.
**Evidence:** #156, #142

### 1.10 Clear stale `*-started` labels before each new cycle
**Why:** Snapshot and clear `*-started` labels at cycle boundaries; only restore if the new cycle label commits successfully.
**Before:** On cycle 2, humans couldn't tell which review stage was actually running.
**Evidence:** #320

---

## 2. CI Runtime Infrastructure

### 2.1 Pre-create all bind-mount source directories on the host
**Why:** Docker silently fails (or errors only at startup) when a bind-mount source path doesn't exist. Always `mkdir -p ~/.config/gh ~/.claude ~/.codex` before launching the devcontainer.
**Evidence:** #77, #78

### 2.2 Inside containers, use `ANTHROPIC_API_KEY` (not `CLAUDE_CODE_OAUTH_TOKEN`)
**Why:** `devcontainers/ci@v0.3` silently drops env vars literally named `CLAUDE_CODE_OAUTH_TOKEN` when forwarding into the container. The GitHub secret can keep its name; rename only at the `env:` block.
**Before:** Three workflows had agents starting with no API key and failing inscrutably.
**Evidence:** #90, #100

### 2.3 Use `WORKFLOW_PAT` as `GH_TOKEN` for `gh` inside devcontainers
**Why:** `github.token` doesn't authenticate `gh pr comment` from inside a `devcontainers/ci@v0.3` container on self-hosted runners.
**Before:** Reviewers ran successfully and posted zero comments. Perfect silent failure.
**Evidence:** #91 (applied across nine occurrences)

### 2.4 Fork PRs stay on `ubuntu-latest`; build images from the `main` ref
**Why:** Untrusted PR code on homelab runners with credential access is a privilege escalation. Building devcontainer images from the PR ref is also a Dockerfile injection vector.
**Evidence:** #110

### 2.5 Set `BUILDX_NO_DEFAULT_ATTESTATIONS=1` when pushing devcontainer images
**Why:** BuildKit's default attestations produce OCI manifest lists without valid platform metadata, breaking `docker push`.
**Evidence:** #133

### 2.6 Wrap `docker run` directly for run steps; reserve `devcontainers/ci@v0.3` for build-and-push only
**Why:** The action's post-step cleanup crashes with "Index was out of range" on self-hosted runners. A custom composite action wrapping `docker run` avoids the bug entirely.
**Evidence:** #179

### 2.7 Bake the Codex CLI into the Dockerfile; route models via LiteLLM with explicit `CODEX_MODEL`
**Why:** Runtime CLI downloads stalled on slow networks; missing model defaults caused fallback to mismatched models. Downgrading three of six review stages to `gpt-5-mini` cut review cost ~45% with no measurable quality loss.
**Evidence:** #110, #130, #145, #292

### 2.8 Auto-detect and strip `SSH_AUTH_SOCK` mount in CI
**Why:** CI runners have no SSH agent, but `devcontainer.json` bind-mounts `${localEnv:SSH_AUTH_SOCK}`, which Docker rejects as an empty source. The CI override config generator must strip this mount before writing.
**Before:** Seven iterative PRs across two days chasing variations of "invalid value for 'source'".
**Evidence:** #224, #226, #230, #248, #260, #270, #280

### 2.9 Validate workflows with `actionlint` + cross-file ref checks + a devcontainer build smoke test
**Why:** Yamllint passes broken `uses:` refs, missing script paths, and local-action checkout races. Over a 48-hour window we shipped 7+ PRs that all passed yamllint and broke at runtime.
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

### 3.2 Heartbeat enforces spec drift, not just job existence
**Why:** Heartbeat compares live cron jobs against canonical specs in `setup/cron/` and patches drift via `CronUpdate`. `pull-main` triggers a fast re-apply when specs change.
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

### 3.5 Pass event IDs through Actions; fetch bodies inside the container
**Why:** Multi-line comment bodies in Actions outputs cause shell-escaping failures and context loss. Pass only `COMMENT_ID`, `ISSUE_NUMBER`, `RUN_ID`, then `gh api` the body inside the devcontainer.
**Evidence:** #41, #178

### 3.6 Reopen-gate PRs until `workflow_run` reviews complete
**Why:** Closing and reopening a PR previously bypassed the review gate because the `all-reviews-passed` check evaluated before reviews actually re-ran.
**Evidence:** #92, #99

### 3.7 Cron specs declare `delivery`, `best-effort-deliver`, and `timeout-seconds` explicitly
**Why:** Missing fields caused accumulating errors and ambiguous routing. Required fields force the spec to be self-describing.
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
