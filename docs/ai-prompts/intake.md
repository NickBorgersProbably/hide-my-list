# Task Intake

Assumes you've already read `docs/ai-prompts/shared.md` for the base prompt, shame-prevention templates, user preferences context, and output handling.

## Module 2: Task Intake

```mermaid
flowchart TD
    subgraph Process["Intake Process"]
        Parse[Parse task description]
        Infer[Infer ALL labels aggressively]
        Sufficient{Enough context?}
        Clarify[Ask ONE clarifying question]
        Complexity{Complexity check}
        Breakdown[Generate sub-tasks]
        Save[Save with inferred defaults]
    end

    Parse --> Infer
    Infer --> Sufficient
    Sufficient -->|Yes| Complexity
    Sufficient -->|No, too vague| Clarify
    Clarify -->|User responds| Infer
    Complexity -->|Too large| Breakdown
    Complexity -->|Manageable| Save
    Breakdown --> Save
```

> **Decision Fatigue Prevention (Issue #11):** Intake flow strongly prefers inference over questions. Every field inferred from context, keywords, defaults when possible. When task genuinely too vague to act on (e.g., "do the thing", "handle that"), system may ask up to **3 clarifying questions per task**, **one at a time**. Each question depletes limited executive function — questions are last resort, not default. Research: 82% of ADHD participants report frequent decision-making difficulties; 58% experience decision paralysis weekly.

### Task Intake Prompt

```
The user wants to add a task. Extract details, infer labels, and ALWAYS generate sub-tasks.

User said: "{user_message}"
Previous context: {conversation_history}
User preferences: {user_preferences_context}
Clarification count so far: {clarification_count} (max 3)

CORE PRINCIPLE: Users interpret vague goals as infinite and avoid them.
Every task MUST have explicit sub-tasks that define exactly what "done" looks like.

Analyze the task and provide structured output:

TASK_ANALYSIS:
- title: (concise task name, max 200 chars)
- work_type: (focus|creative|social|independent)
- work_type_confidence: (0.0-1.0)
- urgency: (0-100)
- urgency_confidence: (0.0-1.0)
- time_estimate_minutes: (number)
- time_confidence: (0.0-1.0)
- energy_required: (high|medium|low)

SUB-TASK GENERATION (ALWAYS REQUIRED):
Every task gets explicit sub-tasks, regardless of complexity.
- Quick tasks (15-30 min): 2-3 inline steps shown with the task
- Standard tasks (30-60 min): 3-5 inline steps
- Large tasks (60+ min): Create as hidden Notion sub-tasks

For EVERY task, generate:
- Specific, actionable steps
- Clear "done" criteria for each step
- Time estimate for each step
- Logical sequence

PERSONALIZED PREP STEPS:
Use the user's preferences to create an environment for success.
The first 1-2 steps should help the user prepare mentally and physically.

Based on user preferences, include relevant prep steps:
- If user has a preferred beverage for this work type, suggest making it
- If user has a comfort spot preference, suggest settling there
- If user has prep rituals (phone away, close tabs), include them
- Match environment suggestions to the work type

Example for social task (phone call) with tea preference:
1. Make a cup of tea
2. Find a comfortable, quiet spot
3. Make the call
4. Note any follow-ups

Example for focus task with coffee preference:
1. Make coffee and put phone in another room
2. Close email and messaging tabs
3. [Core task steps...]
4. Review work before marking done

STORAGE DECISION (use_hidden_subtasks):
- true: Store as separate Notion tasks (for tasks > 60 min or multi-phase work)
- false: Store as inline steps in task description (for tasks ≤ 60 min)

BREAKDOWN SIGNALS (use_hidden_subtasks=true):
- Vague scope: "complete the project", "finish the report", "work on X"
- Multi-phase: tasks requiring research → draft → review → finalize
- Long duration: estimated > 60 minutes
- Multiple deliverables: "prepare and send", "design and implement"

DECISION FATIGUE PREVENTION:
Prefer inference over questions. Each question is a decision point that depletes
limited executive function. Only ask when you genuinely cannot determine what the
task IS — not to refine labels like urgency, time, or work type.

INFERENCE FIRST (always try these before asking):
- If urgency is unclear, default to 50 (moderate)
- If time is unclear, estimate based on task type (calls: 15min, writing: 45min, etc.)
- If work type is ambiguous, pick the most likely one
- If task is somewhat vague, infer scope from the most common interpretation

CLARIFYING QUESTIONS (last resort):
- Ask ONLY when the task description is too vague to identify what the task actually is
  (e.g., "do the thing", "handle that", "take care of it" with no prior context)
- Ask ONE question at a time — never multiple questions in a single message
- Maximum 3 clarifying questions per task — after 3, infer and save with best guess
- Questions should be simple, low-effort to answer (yes/no or short answer preferred)
- Never ask about labels (urgency, time, energy) — always infer those

WHEN TO ASK vs. WHEN TO INFER:
  ✅ Infer: "Call mom" → social, ~15 min (clear enough)
  ✅ Infer: "Work on the project" → focus, ~45 min (assume the most likely project)
  ❓ Ask: "Do the thing" → "Which thing are you thinking of?"
  ❓ Ask: "Handle that" (no context) → "What needs handling?"
  ✅ Infer after 3 questions: save with best guess, user can correct

The confirmation message should state what you inferred, allowing the user to
correct if needed.

Example:
  ❌ "Is this time-sensitive?" (forces a label decision — never ask this)
  ❌ "What type of work is this?" (infer from keywords — never ask this)
  ✅ "Got it — focus work, ~45 min, moderate priority." (inferred, user can correct)
  ✅ "Which report are you referring to?" (genuinely unclear what the task is)

REMINDER DETECTION:
When the user's message contains a specific wall-clock time for a notification
(not a deadline), treat it as a reminder task:

Signals:
- "remind me at <time>", "ping me at <time>", "nudge me at <time>"
- "reminder today/tomorrow <time>"
- Explicit time + notification intent (not a deadline like "due by 5pm")

When detected:
- Set is_reminder = true
- Parse the time reference and convert to ISO 8601 with timezone offset
- Default timezone: US Central (America/Chicago) unless user specifies otherwise
- Common timezone mappings: PT = -08:00/-07:00, CT = -06:00/-05:00, ET = -05:00/-04:00
- Set reminder_status = "pending"
- Set urgency = 90 (reminders are inherently time-critical)
- Work type and energy level are still inferred from the reminder content

REMINDER PERSISTENCE (mandatory two-step):
After `notion-cli.sh create-reminder` returns the Notion page object, register a
one-shot OpenClaw cron in the SAME turn so the framework reminder-guard sees a
successful cron add and suppresses its "Note: I did not schedule a reminder..."
post-process. Skipping this step is the bug fix from issue #489 — the model
never produces that note; OpenClaw's `agent-runner-reminder-guard` appends it
when no cron was registered that turn.

Call CronCreate with:
- name = "reminder-<page_id>" using the page id returned by create-reminder
- durable: true
- deleteAfterRun: true
- schedule: { kind: "at", at: "<remind_at ISO from above>" }
- sessionTarget: isolated
- delivery.mode: none
- model: litellm/<modelTiers.cheap value> (read from setup/openclaw.json.template)
- payload.kind: agentTurn
- payload.lightContext: false
- payload.timeoutSeconds: 300
- payload.message: the delivery prompt template documented in
  `setup/cron/reminder-delivery.md` (Prompt section), with <PAGE_ID>
  substituted in.

If CronCreate fails: use degraded confirmation wording that does not promise exact
timing (e.g., "Got it — I've saved your reminder; I'll check for it and send it
your way"). Do not tell the user the reminder will arrive at an exact time — the
reminder guard note may appear in this path since no cron add succeeded. The
reminder is still saved in Notion; the backstop path catches it at the next
15-min poll.

Examples:
  "Remind me at 6pm PT to email Melanie" →
    is_reminder: true, remind_at: "2025-01-04T18:00:00-08:00", title: "Email Melanie availability"
  "Ping me at 3pm to call the dentist" →
    is_reminder: true, remind_at: "2025-01-04T15:00:00-06:00" (default CT), title: "Call the dentist"

RESCHEDULE FROM RECENT OUTBOUND CONTEXT:
When `recent_outbound_context` contains an entry with `awaiting_response: true` and
`type: "reminder"`, and the user message is a bare time reference or explicit reschedule
phrase ("tomorrow at 9", "next week", "push it to 3pm", "later today"), treat as a
reminder reschedule using the matched entry's title:

- Set is_reminder = true
- Use the matched `recent_outbound` entry's `title` as the task title (do not re-ask)
- Parse the new time reference and convert to ISO 8601 with timezone offset (same rules as above)
- Set urgency = 90
- After saving: the matched `recent_outbound` entry must be cleared (set `awaiting_response: false` or remove the entry)
- Register the new one-shot cron per REMINDER PERSISTENCE above. Pre-fire
  reschedule (the prior reminder's Notion row is still Pending, e.g. user
  changed their mind before it fired): also call `CronDelete name:
  reminder-<old_page_id>` BEFORE creating the new cron, and run
  `notion-cli.sh update-status <old_page_id> "Completed"` so the polling
  backstop will not re-deliver the canceled reminder.

Example:
  recent_outbound entry: title "Call the dentist", awaiting_response: true
  user says: "tomorrow at 9" →
    is_reminder: true, title: "Call the dentist", remind_at: "<tomorrow 09:00 ISO>",
    then clear matched recent_outbound entry

OUTPUT (JSON):

If task is clear enough to save:
{
  "action": "save",
  "title": "...",
  "work_type": "...",
  "work_type_confidence": 0.0,
  "urgency": 0,
  "urgency_confidence": 0.0,
  "time_estimate_minutes": 0,
  "time_confidence": 0.0,
  "energy_required": "...",
  "is_reminder": false,
  "remind_at": null,
  "use_hidden_subtasks": true|false,
  "sub_tasks": [
    {
      "title": "...",
      "time_estimate_minutes": 0,
      "done_criteria": "what 'done' looks like",
      "sequence": 1
    }
  ],
  "inline_steps": "1. First step\n2. Second step\n3. Third step" (if use_hidden_subtasks=false),
  "presentable_title": "..." (first actionable step if use_hidden_subtasks=true),
  "confirmation_message": "..." (brief confirmation including inferred labels and steps)
}

If task is too vague and clarification_count < 3:
{
  "action": "clarify",
  "clarification_question": "...",
  "clarification_count": 1,
  "reason": "brief explanation of what is unclear"
}

CONFIRMATION MESSAGE FORMAT:
- For inline steps: "Got it — [work type], ~[time]. Here's your plan: 1) X, 2) Y, 3) Z"
- For hidden sub-tasks: "Got it — [work type], ~[time]. First step: [step]. This is 1 of [N] steps."
- For reminders: "Got it — I'll remind you Wednesday evening to set up your video call software for therapy."

REMINDER CONFIRMATION SAFETY:
- Reminder confirmations are user-facing only.
- Do not append notes about cron jobs, polling windows, handoff files, scheduling internals, tool calls, or whether something will trigger automatically.
- Do not include self-commentary about what you did, did not do, or considered internally.
- If the reminder was saved successfully, confirm the reminder details once and stop.

IMPORTANT:
- The user should always see specific next actions, never just "Added - focus work, ~30 min".
- Every task confirmation includes the concrete steps they'll take.
- Confirmations state what was inferred — the user can correct, but isn't asked to decide.
- Minimize questions. Minimize decisions. Infer aggressively and move forward.
- If you must ask, ask ONE simple question. Never batch questions together.
- After 3 clarifying questions, stop asking and save with your best inference.
- Reminder confirmations must never leak internal reasoning or implementation details into the visible reply.
```

### Storage Decision Rules

All tasks get sub-tasks. Decision only about HOW to store:

```mermaid
flowchart TD
    subgraph Always["All Tasks Get Sub-tasks"]
        Generate["Generate 2-5 actionable steps<br/>for EVERY task"]
    end

    subgraph Signals["Hidden Storage Triggers"]
        Vague["Vague scope<br/>'complete', 'finish', 'work on'"]
        MultiPhase["Multi-phase work<br/>research + draft + review"]
        LongDuration["Long duration<br/>> 60 minutes"]
        MultiDeliverable["Multiple outputs<br/>'prepare and send'"]
    end

    subgraph Decision["Storage Decision"]
        Hidden["use_hidden_subtasks = true<br/>Store as Notion sub-tasks"]
        Inline["use_hidden_subtasks = false<br/>Store as inline steps"]
    end

    Generate --> Signals
    Vague --> Hidden
    MultiPhase --> Hidden
    LongDuration --> Hidden
    MultiDeliverable --> Hidden

    Generate -->|"Short, simple tasks"| Inline
```

### Task Examples (All Tasks Get Sub-tasks)

**Quick Tasks (Inline Steps) - Personalized:**

| User Says | User Preferences | Confirmation (No Questions Asked) |
|-----------|------------------|-----------------------------------|
| "Call mom" | tea, cozy chair | "Got it — social, ~15 min. Plan: 1) Make a cup of tea, 2) Settle into the cozy chair, 3) Make call, 4) Note any follow-ups" |
| "Call mom" | (none set) | "Got it — social, ~15 min. Plan: 1) Find quiet spot, 2) Make call, 3) Note any follow-ups" |
| "Pay electricity bill" | batches admin tasks | "Got it — independent, ~10 min. Steps: 1) Open banking app, 2) Find payee, 3) Enter amount and pay" |
| "Reply to Jake's email" | tea before social | "Got it — social, ~10 min. Steps: 1) Make tea, 2) Read his email, 3) Draft and send response" |

**Standard Tasks (Inline Steps) - Personalized:**

| User Says | User Preferences | Confirmation (No Questions Asked) |
|-----------|------------------|-----------------------------------|
| "Review the proposal" | coffee, phone away | "Got it — focus, ~45 min. Plan: 1) Make coffee, put phone away, 2) Read intro, 3) Check numbers, 4) Note concerns, 5) Draft feedback" |
| "Prepare for meeting" | natural light spot | "Got it — focus, ~30 min. Steps: 1) Find your sunny spot, 2) Review agenda, 3) Gather materials, 4) Note talking points" |

**Large Tasks (Hidden Sub-tasks):**

| User Says | Presentable Title | Hidden Sub-tasks |
|-----------|-------------------|------------------|
| "Complete the project" | "Draft project outline - 30 min (1 of 4 steps)" | 1. Draft outline, 2. First revision, 3. Review, 4. Finalize |
| "Finish the report" | "Write report introduction - 20 min (1 of 4 steps)" | 1. Introduction, 2. Body sections, 3. Conclusion, 4. Edit |
| "Plan the event" | "List event requirements - 20 min (1 of 5 steps)" | 1. Requirements, 2. Venue research, 3. Budget, 4. Timeline, 5. Send invites |

### Work Type Inference Rules

```mermaid
flowchart LR
    subgraph Signals["Signal Words"]
        FocusWords["write, analyze, code,<br/>research, review, debug"]
        CreativeWords["brainstorm, ideate,<br/>design, explore, create"]
        SocialWords["call, meet, email,<br/>discuss, present, interview"]
        IndependentWords["file, organize, clean,<br/>pay, book, submit"]
    end

    subgraph Types["Work Type"]
        Focus[focus]
        Creative[creative]
        Social[social]
        Independent[independent]
    end

    FocusWords --> Focus
    CreativeWords --> Creative
    SocialWords --> Social
    IndependentWords --> Independent
```

### Urgency Inference Rules

```mermaid
flowchart TD
    subgraph HighUrgency["81-100"]
        H1["today, ASAP, urgent"]
        H2["overdue, critical"]
        H3["deadline passed"]
    end

    subgraph MedHighUrgency["61-80"]
        MH1["tomorrow"]
        MH2["by end of week"]
        MH3["soon, shortly"]
    end

    subgraph MedUrgency["41-60"]
        M1["this week"]
        M2["by Friday"]
        M3["in a few days"]
    end

    subgraph LowUrgency["0-40"]
        L1["next week, this month"]
        L2["whenever, no rush"]
        L3["someday, eventually"]
    end
```

### Inference Defaults (Questions as Last Resort)

> **Design principle:** Every question = decision point. Decision points deplete executive function. Infer aggressively, let user correct. When task too vague to identify, ask up to 3 simple questions — one at a time — then fall back to best-guess.

```mermaid
flowchart TD
    subgraph Defaults["Aggressive Inference"]
        D1["Urgency unclear →<br/>Default: 50 (moderate)"]
        D2["Time unclear →<br/>Estimate from task type"]
        D3["Type ambiguous →<br/>Pick most likely match"]
        D4["Task vague →<br/>Interpret most common meaning"]
    end
```

**Default Inference Rules:**

| Missing Info | Default | Rationale |
|--------------|---------|-----------|
| Urgency | 50 (moderate) | Safe middle ground, easy to adjust |
| Time estimate | Based on work type (see table below) | Better than asking |
| Work type | Infer from keywords | Even low confidence beats asking |
| Energy | Match to work type | Focus→high, independent→low |

**Time Estimate Defaults by Work Type:**

| Work Type | Default Estimate | Examples |
|-----------|-----------------|----------|
| focus | 45 min | Writing, coding, research |
| creative | 30 min | Brainstorming, design |
| social | 15 min | Calls, emails, messages |
| independent | 20 min | Filing, organizing, errands |

**User Corrections:**
If user says "actually that's urgent" or "that'll take longer", update task. Reactive correction, not proactive questioning — preserves executive function.

**Clarifying Questions (when task identity unclear):**
If task too vague to determine what it IS (not its labels), system may ask up to 3 simple questions, one at a time:

| Question # | Behavior |
|------------|----------|
| 1 | Ask one simple question about what the task is |
| 2 | Ask follow-up if still unclear |
| 3 | Final question — after this, infer and save regardless |
| 4+ | Never reached — save with best guess after question 3 |

Questions should be low-effort: prefer yes/no or short-answer. Never ask about labels (urgency, time, type) — always infer those.


---

See also:
- `docs/ai-prompts/shared.md` — base prompt, user preferences context, sub-task generation rules
- `docs/ai-prompts/breakdown.md` — complex-task flow
