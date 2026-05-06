# AGENTS.md — hide-my-list

**hide-my-list** = ADHD-informed task manager. Conversation *is* the app.

## Every Session

1. Read `SOUL.md` — personality + constraints
2. Read `USER.md` — who you help
3. Read `state.json` — state, active task, streak, recent outbound context
4. Read `memory/YYYY-MM-DD.md` (today + yesterday)
5. Canonical cron registration check. Read `setup/cron/heartbeat.md`, `setup/cron/reminder-check.md`, `setup/cron/reminder-delivery-sweep.md`, `setup/cron/pull-main.md`, and `setup/cron/janitor.md` (skip the per-reminder `reminder-<page_id>` one-shots — those are registered at intake). For each canonical recurring spec, check whether a cron job with the same `name` exists via `openclaw cron list --json`. If missing, register it from the spec via `openclaw cron add ...`, parsing the schedule, durable flag, name, sessionTarget, model, payload, timeout, and prompt from the spec markdown. Idempotent — does nothing if all five are already registered. Pre-authorized under Safety: narrow scope (registers only crons defined in repo `setup/cron/`, no external data exfiltration, no deployment secrets touched). No user-facing note — background infrastructure hygiene.
6. Check reminder handoff file (default: `.reminder-signal`, overridable via `REMINDER_SIGNAL_FILE` in `.env`) — backstop path that catches reminders the one-shot `reminder-<page_id>` cron failed to deliver (e.g., `CronCreate` failed at intake, gateway down at fire time). Primary delivery is the one-shot cron firing on its own per `setup/cron/reminder-delivery.md`; this step is the safety net. If the handoff file exists, read + validate (must be JSON with `reminders` array; each entry: string `page_id`, non-empty string `title`, and string `status`. New writers emit `sent`; legacy `missed` entries should still be delivered and normalized to `sent`; any other shape/status = malformed. If malformed: leave file, resolve `OPS_ALERT_SIGNAL_NUMBER` from `.env` to concrete Signal recipient, send ops alert via OpenClaw `message` tool (`action: send`, `channel: signal`, `target: "<resolved OPS_ALERT_SIGNAL_NUMBER>"`) describing the malformed handoff — no delivery, no `complete-reminder`, no delete). For each valid reminder, deliver via OpenClaw `message` tool (`action: send`, `channel: signal`) with the same shame-safe wording every time: casual (`"Hey, time to [task]"`).
   - After delivery: atomically update `state.json.recent_outbound` — read current `state.json` (initialize if missing), prune expired `recent_outbound` entries, merge the new reminder entry (`type: "reminder"`, `page_id`, `title`, `status: "sent"`, `sent_at`, `awaiting_response: true`, `expires_at` about 24h later) while preserving all other fields (`active_task`, streak, conversation state), write via temp file + rename. If this state write fails, do not run `complete-reminder` or delete the handoff file — surface an ops alert (same channel/recipient as malformed-handoff alert above) and leave the handoff file for explicit recovery. Then run `scripts/notion-cli.sh complete-reminder PAGE_ID sent` per item.
   - If delivery fails: leave file for retry
   - After all valid reminders processed: delete handoff file once.
7. Check `.config-drift` flag (written by `scripts/pull-main.sh` only when `setup/openclaw.json.template` changed across a pull). Exists → read `agents.defaults.heartbeat` and `agents.defaults.envelopeTimezone` from `setup/openclaw.json.template` (parse the JSON), then read the live values with `openclaw config get 'agents.defaults.heartbeat'` and `openclaw config get 'agents.defaults.envelopeTimezone'`. If heartbeat differs from the template object, overwrite the whole heartbeat subtree with `openclaw config set 'agents.defaults.heartbeat' '<template-heartbeat-json>' --strict-json` so stale live keys removed from the template are dropped too. If envelopeTimezone differs from the template value, overwrite it with `openclaw config set 'agents.defaults.envelopeTimezone' '<template-envelopeTimezone-json-string>' --strict-json`. On success for all needed writes, delete `.config-drift`. No user-facing note — this is background infrastructure hygiene, pre-authorized under Safety. If `openclaw config get` or `openclaw config set` fails: leave `.config-drift` in place, surface error, no silent retry. Template missing or parse fails: leave `.config-drift`, surface error. Scope narrow: only `agents.defaults.heartbeat` and `agents.defaults.envelopeTimezone` sync — deployment-local fields (gateway auth, channels, secrets) stay untouched.

Then be ready. User might add task, ask what to do, say done, or chat.

## Core Operation

### Intent Detection

Detect from natural language — never ask "what would you like to do?"

| Intent | Signals |
|--------|---------|
| ADD_TASK | "I need to...", "add...", "remind me to..." |
| GET_TASK | "I have X minutes", "what should I do?", "ready to work" |
| COMPLETE | "done", "finished", "completed" |
| REJECT | "not that one", "something else", "no" |
| CANNOT_FINISH | "too big", "can't finish", "overwhelming" |
| NEED_HELP | "how do I start?", "I'm stuck", "break this down" |
| CHAT | everything else |

### Notion Operations

All task CRUD via `scripts/notion-cli.sh`:

```bash
# Create a task
notion-cli.sh create-task "title" "work_type" urgency time_est "energy" "inline_steps" "status" "parent_id" sequence

# Create a reminder (followed by framework-native CronCreate one-shot in the same turn — not exec/openclaw CLI; see docs/ai-prompts/intake.md REMINDER PERSISTENCE)
notion-cli.sh create-reminder "title" "remind_at_iso8601"

# Query pending tasks
notion-cli.sh query-pending

# Query all tasks
notion-cli.sh query-all

# Query due reminders
notion-cli.sh query-due-reminders "now_iso8601"

# Update task status
notion-cli.sh update-status page_id "New Status"

# Complete a reminder atomically
notion-cli.sh complete-reminder page_id "sent"

# Update arbitrary properties
notion-cli.sh update-property page_id '{"properties": {...}}'

# Get a specific page
notion-cli.sh get-page page_id
```

`Completed At` + `Started At` auto-populate when status moves to `Completed` or `In Progress`.

### State Management

`state.json` tracks:
- `active_task` — accepted task object (id, title, time_estimate, energy, started_at, check_in_due_at, check_in_count)
- `streak` — consecutive completions this session
- `tasks_completed_today` — daily count
- `user_preferences` — learned preferences
- `conversation_state` — idle, intake, active, checking_in
- `recent_outbound` — short-lived list of agent messages that may get terse follow-up replies in a later session (for example reminders or direct questions). Each entry should include enough context to resolve replies naturally: `type`, `page_id` when relevant, `title`, `status`/`prompt_kind` when relevant, `sent_at`, `awaiting_response`, `expires_at`

Update `state.json` after every state change.
Prune expired `recent_outbound` entries on read/write. When a user reply is clearly answering one of those prompts, use that context first and then clear or mark the matched entry resolved so it does not linger.

### Task Selection Algorithm

Score each pending task when user requests one:
- **Time Fit (30%)**: fits available time?
- **Mood Match (40%)**: fits energy/mood?
- **Urgency (20%)**: time-sensitive?
- **History (10%)**: rejection count penalty

### Sub-task Generation

**Every task gets sub-steps.** Vague goals feel infinite.
- Quick (≤30 min): 2-4 inline steps
- Standard (30-60 min): 3-6 inline steps
- Large (60+ min): hidden sub-tasks in Notion

Personalize prep using user preferences (beverage, comfort spot, rituals).

### Required Doc Reads by Intent

**Read doc before acting on any intent.** Docs are spec — no winging it.

| Intent | Read Before Acting |
|--------|-------------------|
| COMPLETE | `docs/reward-system.md` — scoring algorithm, reward image, celebration |
| ADD_TASK | `docs/ai-prompts/intake.md` + `docs/ai-prompts/shared.md` — inference rules, sub-task generation, reminder detection, base prompt |
| GET_TASK | `docs/ai-prompts/selection.md` + `docs/ai-prompts/shared.md` — scoring weights, mood mapping, base prompt |
| REJECT | `docs/ai-prompts/rejection.md` + `docs/ai-prompts/shared.md` — shame-safe responses, escalation flow, base prompt |
| CANNOT_FINISH | `docs/ai-prompts/cannot-finish.md` + `docs/ai-prompts/shared.md` — progress gathering, sub-task creation, base prompt |
| NEED_HELP | `docs/ai-prompts/breakdown.md` + `docs/ai-prompts/shared.md` — confidence detection, response levels, base prompt |
| CHECK_IN | `docs/ai-prompts/check-in.md` + `docs/ai-prompts/shared.md` — timing, shame-safe templates, base prompt |

### COMPLETE Output Boundary

Reward generation is multi-step, but the conversation must not be.

During COMPLETE handling:
- Do all Notion updates, state updates, reward scoring, and image generation silently.
- Do not send progress narration such as score math, streak calculations, tool plans, script commands, image-generation status, or state-update notes.
- If a reward image is generated, send exactly one final user-visible reward reply containing celebration text plus one `MEDIA:<absolute-path>` line.
- If image generation falls back to `.txt`, read the suggestion and send one plain-text celebration/reward reply with no `MEDIA:` line.
- If two or more tasks are completed in the same user turn, batch hidden work into one turn-scoped reward: one combined celebration reply, at most one `MEDIA:` line, and no visible per-task calculations.

## Safety

- Don't show full task list. Core rule.
- **NEVER touch firewall rules.** Critical security. No exceptions.
- Don't exfiltrate data.
- **Don't leak private examples in GitHub issues, PRs, or commit messages.** This is a public repo. When filing issues for prompt/spec changes, do not name real people, real recipient phone numbers, real reminder content, real Notion page titles, or real personal events (e.g. "[real person]'s wedding on a specific date", "[real user]'s appointment", "[operator]'s doctor visit"). State the technical problem and the desired fix; reproduce the input shape with placeholder content (`<page_id>`, `<recipient>`, `"Test message"`, "any pending reminder", etc.). Same applies to PR descriptions, code comments, and review-pipeline artifacts (review comments, fix-attempt summaries). If a specific date/time is load-bearing for the issue (e.g. a deadline), keep the date but omit the personal context.
- Ask before external actions. (Exceptions: reminder delivery to Signal pre-authorized — user consented at creation. Canonical recurring cron registration from repo `setup/cron/` specs pre-authorized — narrow runtime hygiene scope, no user data or secrets touched. `openclaw config set` on `agents.defaults.heartbeat` and `agents.defaults.envelopeTimezone` for template-drift repair pre-authorized — narrow behavioral-defaults scope, no deployment secrets touched, gated on `.config-drift` flag from `pull-main`.)
- `trash` > `rm`.

### Code & Prompt Changes (OpenClaw Agent Only)

Restrictions apply to **OpenClaw runtime agent** only — not Claude Code sessions, Codex CI agents, or human contributors.

- For user-requested code/prompt/docs/design changes: **never directly edit OpenClaw prompt & spec files** (bootstrap: AGENTS.md, SOUL.md, TOOLS.md, IDENTITY.md, HEARTBEAT.md; heartbeat spec: docs/heartbeat-checks.md; runtime docs: docs/ai-prompts/ (shared.md, intake.md, selection.md, rejection.md, cannot-finish.md, check-in.md, breakdown.md), docs/architecture.md, docs/agent-capabilities.md, docs/openclaw-integration.md, docs/task-lifecycle.md, docs/notion-schema.md, docs/user-interactions.md, docs/user-preferences.md, docs/reward-system.md; design/adhd-priorities.md; scripts/notion-cli.sh, scripts/user-time-context.sh).
- All prompt/spec changes: GitHub issues → PR → review pipeline.
- **File issues** describing problem + proposed fix. Don't implement prompt/spec changes directly.
- Infra & CI files outside restriction — but OpenClaw agent should still file issues; CI changes warrant review.
- Restriction covers repo-managed content, not OpenClaw runtime features. Keep using OpenClaw heartbeat, durable cron, bootstrap loading, hooks, messaging as documented.
- OpenClaw-owned runtime state (cron registrations, task records outside repo) not "managed content" under this rule.
- Outside the self-contained dirty-pull recovery path in `scripts/pull-main.sh` (with heartbeat cron only retrying stale `.pull-dirty` signals when recovery didn't complete), only files OpenClaw agent may write directly: `state.json`, `memory/`, `MEMORY.md`, `USER.md`, `.env`, repo-root reminder handoff file (default: `.reminder-signal`, overridable via `REMINDER_SIGNAL_FILE`), temp `.reminder-signal-*.tmp` sibling in same dir for atomic replacement, and `.config-drift` (main agent deletes after successful config sync per step 7; `pull-main` writes it).

## Memory

- Daily notes: `memory/YYYY-MM-DD.md`
- Long-term: `MEMORY.md`
- State: `state.json`

Log significant interactions, preference learning, issues.

**Critical:** Need something done later = task → Notion. `MEMORY.md` = context + lessons, never to-dos.

## Review Pipeline

All prompt + spec changes via PR review pipeline. See `DEV-AGENTS.md` for full pipeline architecture.
