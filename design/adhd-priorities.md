# ADHD-Informed Feature Prioritization for Hello World

## Executive Summary

This document analyzes the eight open ADHD feature requests (Issues #5-#12) through the lens of ADHD psychology research, classifying each as **foundational** (must shape core architecture from day one) or **additive** (can be layered on later). The goal is to ensure the hello world implementation embeds the right data structures, interaction patterns, and extensibility points so that critical ADHD accommodations are not retrofitted but native.

---

## Classification: Foundational vs. Additive

### Foundational Features (Must Shape Core Architecture)

These features address the most disabling ADHD symptoms and require data structures, interaction patterns, or architectural decisions that are expensive to retrofit.

| Priority | Issue | Feature | Core ADHD Mechanism |
|----------|-------|---------|---------------------|
| F1 | #10 | Shame Prevention / RSD-Aware Messaging | Rejection Sensitive Dysphoria |
| F2 | #11 | Decision Fatigue Prevention | Executive Function Depletion |
| F3 | #7 | Task Initiation Rewards | Dopamine-Deficient Reward System |
| F4 | #6 | Time Blindness Compensation | Impaired Time Perception |

### Additive Features (Can Be Layered On Later)

These features enhance the experience but do not require fundamental architectural changes. They extend patterns established by foundational features.

| Priority | Issue | Feature | Core ADHD Mechanism |
|----------|-------|---------|---------------------|
| A1 | #8 | Transition/Task-Switching Support | Cognitive Flexibility Deficits |
| A2 | #9 | Waiting Mode Recognition | Anticipatory Paralysis |
| A3 | #12 | Novelty in Rewards | Habituation / Stimulation Seeking |
| A4 | #5 | Body Doubling / Accountability | Social Dopamine Stimulation |

---

## Detailed Rationale

### F1: Shame Prevention / RSD-Aware Messaging (Issue #10) -- HIGHEST PRIORITY

**Why foundational:** RSD is the single biggest risk to user retention. If the system triggers shame at any point -- through tone, implied judgment, or framing failure as personal -- the user may never return. This is not a feature you add; it is a **constraint on every message the system ever generates**. It must be embedded in the system prompt, the response templates, the rejection flow, the check-in flow, and the cannot-finish flow from the very first line of code.

**Research basis:**
- 70% of ADHD adults experience RSD (Cleveland Clinic)
- RSD triggers include "falling short of expectations" -- a task manager is inherently a system of expectations
- Users with RSD may "stop trying" at the first hint of failure, meaning a single poorly-worded message can end engagement permanently

**What this means for architecture:**
- The base system prompt (documented in ai-prompts.md) must encode shame-prevention as a hard constraint, not a soft tone guideline
- Every AI response path -- completion, rejection, cannot-finish, check-in, abandoned task -- needs explicit RSD-safe framing
- The system must never use language that implies the user has failed, is behind, or should have done better
- Rejection must be reframed as "information that helps me serve you better," never as user failure
- The conversation state model should track consecutive difficulties (multiple rejections, abandonments) and escalate to explicit normalization rather than continuing to push tasks

**Data structures needed from day one:**
- `session_difficulty_count`: Track consecutive rejections/abandonments within a session to trigger explicit normalization
- `emotional_state_signals`: Store signals from user messages (frustration, shame, overwhelm) alongside intent detection
- RSD-safe message templates must be the **only** templates, not an alternative mode

**Recommendation for architect:** The AI prompt system must treat shame prevention as a first-class architectural constraint. Every prompt module (intent detection, task intake, selection, rejection, check-in, cannot-finish) must include explicit RSD-safe instructions. This is not optional -- it is the difference between a tool that helps and one that harms.

**Recommendation for DevOps:** Automated testing should include "adversarial emotional scenarios" -- e.g., user rejects 5 tasks in a row, user says "I'm useless," user abandons mid-task repeatedly. The system's responses in these scenarios should be validated as shame-free.

---

### F2: Decision Fatigue Prevention (Issue #11) -- SECOND PRIORITY

**Why foundational:** The current task intake design (documented in ai-prompts.md and task-lifecycle.md) can ask up to three clarifying questions (urgency, time, work type). Each question is a decision that depletes executive function. With 82% of ADHD individuals reporting decision-making difficulties and 58% experiencing paralysis weekly, the intake flow must default to **aggressive inference** from the very first implementation.

**Research basis:**
- Decision fatigue in ADHD stems from "the exhausting need to make continuous choices"
- Each decision point consumes limited executive function resources
- Decision paralysis can prevent a user from ever completing task intake

**What this means for architecture:**
- Task intake must work with **zero questions** as the default path, not as an optional mode
- The AI should infer all labels (work type, urgency, time estimate, energy) with best-guess defaults
- Confirmation should use accept/reject patterns ("I'm marking this as moderate urgency -- sound right?") rather than open-ended questions
- The `needs_clarification` path in the current prompt design should be the exception, not the norm
- When clarification is truly needed, offer constrained choices ("Quick / Medium / Long") rather than open questions

**Data structures needed from day one:**
- `inference_confidence` per label: Store confidence scores so the system can learn which types of tasks need clarification
- `user_defaults`: Learned defaults per work type (e.g., "user's focus tasks average 45 min")
- `intake_completion_rate`: Track how often users abandon intake mid-flow to detect question fatigue

**Recommendation for architect:** The task intake prompt should be redesigned to default to zero-question intake. The current prompt's `needs_clarification` threshold should be very low (perhaps < 0.3 confidence, not < 0.5). The system should tolerate imprecise labels in exchange for faster, frictionless intake.

**Recommendation for DevOps:** Track intake abandonment rate as a key metric. If users start describing tasks but never complete intake, the system is asking too many questions.

---

### F3: Task Initiation Rewards (Issue #7) -- THIRD PRIORITY

**Why foundational:** The current reward system (reward-system.md) only fires on task completion. But ADHD research is unambiguous: **starting is harder than finishing**. Dopamine is needed at the moment of initiation, not 30 minutes later when the task is done. If the reward architecture only supports completion triggers, adding initiation rewards later requires restructuring the entire reward engine.

**Research basis:**
- "Getting started is often the most difficult part" (Healing Psychiatry Florida)
- Low dopamine makes task initiation specifically difficult (Tiimo research)
- ADHD reward systems need "immediate reinforcement" -- delayed rewards don't register equivalently

**What this means for architecture:**
- The reward engine must support **multiple trigger points** in the task lifecycle, not just `completed`
- Minimum trigger points from day one: `started`, `first_step_completed`, `resumed_after_break`, `completed`
- Initiation rewards should be qualitatively different from completion rewards (encouragement vs. celebration)
- The `RewardEngine` component needs a `trigger_type` parameter, not just an intensity scalar

**Data structures needed from day one:**
- `task_started_at`: Timestamp when user accepts task (already partially modeled as `in_progress` status)
- `steps_completed`: Track sub-step progress for first-step celebrations
- `resume_count`: How many times a user returned to a task after stepping away
- `reward_trigger_type`: enum of `initiation | first_step | resume | completion | streak | milestone`

**Recommendation for architect:** The reward engine interface should accept a `TriggerType` enum from the start, even if only `completion` is initially implemented. This prevents the engine from being hardcoded to a single trigger pattern.

**Recommendation for DevOps:** The celebration message store (templates, GIFs, etc.) should be structured as a content database keyed by trigger type, not a flat list. This enables per-trigger-type content from day one.

---

### F4: Time Blindness Compensation (Issue #6) -- FOURTH PRIORITY

**Why foundational:** Time blindness is a core neurological symptom, not a behavioral preference. The current system has check-in timers at 1.25x estimated time, but it does not track actual-vs-estimated time or apply time estimate multipliers. These patterns require data collection from the very first task to be useful -- you cannot retroactively learn a user's time estimation bias.

**Research basis:**
- Russell Barkley identifies time blindness as intrinsic to ADHD neurology
- "Time must be externalized" to keep it within awareness (UCI Health)
- Time perception is linked to dopamine dysregulation

**What this means for architecture:**
- Every task must record `estimated_duration` and `actual_duration` from the very first use
- A per-user `time_multiplier` should be computed and updated after each task
- The frontend must support optional visible elapsed time during active tasks
- Time estimates shown to users should apply the learned multiplier transparently

**Data structures needed from day one:**
- `estimated_duration_minutes`: Already exists in the task model
- `actual_duration_minutes`: Must be computed from `started_at` and `completed_at` timestamps
- `user_time_multiplier`: Per-user adjustment factor, starting at 1.5 (research-supported ADHD default)
- `time_estimate_history`: Array of `{estimated, actual, work_type}` tuples for learning

**Recommendation for architect:** Record `started_at` and `completed_at` timestamps on every task from day one, even if the time learning algorithm is not yet implemented. The data collection is cheap; the inability to backfill it is expensive.

**Recommendation for DevOps:** Consider adding a simple dashboard metric showing aggregate estimated vs. actual times across all tasks, to validate the time multiplier system once implemented.

---

### A1: Transition/Task-Switching Support (Issue #8)

**Why additive:** The `transition_ritual` preference already exists in the user preferences model. Transition support primarily requires changes to conversation flow (adding a pause between completion and next-task suggestion) and prompt templates. These are behavioral changes, not architectural ones.

**What to build now:** Ensure the conversation state model has a `post_completion` state that is distinct from `idle`, so a pause can be inserted later. The current state machine goes directly from `Completed` to offering the next task.

---

### A2: Waiting Mode Recognition (Issue #9)

**Why additive:** Waiting mode is a real and significant ADHD experience, but supporting it requires calendar integration (which is not in the hello world scope) and a specialized task-filtering algorithm. The core task selection scoring can accommodate this later by adding a `waiting_mode` context flag that biases toward short, interruptible tasks.

**What to build now:** Ensure the task selection prompt accepts optional context flags (like `waiting_mode: true`) that can modify scoring weights. This is a parameter, not an architectural change.

---

### A3: Novelty in Rewards (Issue #12)

**Why additive:** Novelty is important for sustained engagement, but it is a content and algorithm problem, not an architectural one. The reward system needs a content pool and a freshness-tracking mechanism, both of which can be added to the existing reward engine without restructuring it.

**What to build now:** Ensure the reward engine's content selection is abstracted (not hardcoded GIF URLs or message strings), so a content pool with freshness tracking can be swapped in later.

---

### A4: Body Doubling / Accountability (Issue #5)

**Why additive:** Body doubling is highly effective but requires external integrations (two-way messaging with accountability partners, possible virtual coworking platform integration). The current architecture's one-way SMS notification is a good foundation. Making it two-way is an integration task, not an architectural redesign.

**What to build now:** Ensure the notification system supports `trigger_type` (not just completion) so that `task_started` notifications can be added later for accountability.

---

## Hello World Design Recommendations

### 1. Data Structures Required from Day One

The hello world must include these fields in the task model even if the corresponding features are not fully implemented:

```
Task {
    // Existing fields from docs
    id, title, work_type, urgency, time_estimate_minutes,
    energy_required, status, parent_id, inline_steps,
    rejection_count, rejection_notes

    // Required for time blindness (F4)
    started_at: timestamp
    completed_at: timestamp

    // Required for initiation rewards (F3)
    steps_completed: integer
    resume_count: integer
}

UserState {
    // Required for RSD prevention (F1)
    session_difficulty_count: integer
    consecutive_rejections: integer

    // Required for time blindness (F4)
    time_multiplier: float (default 1.5)

    // Required for decision fatigue (F2)
    learned_defaults: {
        typical_urgency_by_work_type: map
        typical_duration_by_work_type: map
    }
}

RewardTrigger {
    // Required for initiation rewards (F3)
    trigger_type: enum(initiation, first_step, resume, completion, streak, milestone)
    intensity: float
    task_id: reference
}
```

### 2. Interaction Patterns Required from Day One

**Zero-question intake as default:** The very first task intake experience should work with zero clarifying questions. The AI infers everything and confirms with accept/reject options.

**Shame-free messaging as a hard constraint:** Every response template and AI prompt must be reviewed against RSD criteria. The system should never:
- Use "you should have," "you didn't," or "you failed"
- Imply a deadline was missed or the user is behind
- Frame task rejection as negative
- Use countdown language that creates pressure

**Celebrate starting, not just finishing:** The first time a user accepts a task, the system should acknowledge the effort of initiation with a brief, warm response distinct from the completion celebration.

### 3. Conversation State Machine Extensions

The current state machine (`Idle -> Intake -> Selection -> Active -> Completed`) needs these additions for the hello world:

```
Active -> PostCompletion -> Idle       (transition pause, A1)
Selection -> Active (with initiation acknowledgment, F3)
Active -> Struggling (detected from difficulty signals, F1)
Struggling -> Normalized -> Active|Idle (explicit RSD-safe exit, F1)
```

The `Struggling` and `Normalized` states support the RSD prevention system by detecting when a user is having difficulty and providing explicit shame-free messaging before continuing.

### 4. AI Prompt Constraints

Add to the base system prompt:

```
SHAME PREVENTION (MANDATORY):
- Never imply the user has failed or fallen short
- Frame all difficulties as information, not shortcomings
- If user rejects 3+ tasks, explicitly normalize: "Sometimes the brain isn't in task mode"
- If user can't finish, always acknowledge progress first
- If user seems frustrated, offer a graceful exit without judgment
- Never use urgency language that creates pressure or guilt
- Rejection is the user helping you suggest better -- say so

DECISION MINIMIZATION (MANDATORY):
- Default to zero clarifying questions during intake
- Infer all labels from context; confirm with accept/reject, not open questions
- If you must ask one question, offer 2-3 constrained choices, not open-ended
- Accept imprecise information -- a roughly-labeled task is better than an abandoned intake
```

### 5. Extensibility Points for Additive Features

The hello world should include these extension points even without implementing the additive features:

| Extension Point | Enables | Implementation |
|----------------|---------|----------------|
| `post_completion` conversation state | Transition support (A1) | Add state to enum, no logic yet |
| Optional context flags on task selection | Waiting mode (A2) | Accept but ignore unknown flags |
| Abstracted reward content selection | Novelty system (A3) | Content behind an interface, not inline |
| `trigger_type` on notification events | Body doubling (A4) | Enum field on notification model |

---

## Priority Summary

```
MUST HAVE in hello world:
  [F1] RSD-safe messaging baked into every prompt and template
  [F2] Zero-question default intake with aggressive inference
  [F3] Reward trigger type enum supporting initiation events
  [F4] Timestamp recording for time blindness learning

DESIGN FOR but don't implement:
  [A1] Post-completion pause state in conversation model
  [A2] Context flags on task selection for waiting mode
  [A3] Abstracted reward content selection
  [A4] Trigger type on notification events
```

The overarching principle: **the hello world should feel safe, effortless to use, and rewarding from the very first interaction**. Safety (F1) prevents harm. Effortlessness (F2) prevents abandonment. Reward at initiation (F3) creates the dopamine bridge to action. Time awareness (F4) builds the data foundation for long-term learning. Everything else extends these four pillars.
