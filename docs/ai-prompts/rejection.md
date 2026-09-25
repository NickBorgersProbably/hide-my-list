# Rejection Handling

Assumes you've already read `docs/ai-prompts/shared.md` for the base prompt, shame-prevention templates, user preferences context, and output handling.

## Module 4: Rejection Handling

```mermaid
flowchart TD
    Reject([User rejects task]) --> Ask[Ask for reason]
    Ask --> Reason[User provides reason]
    Reason --> Classify[Classify rejection type]
    Classify --> Update[Update task in Notion]
    Update --> Reselect[Select alternative]
    Reselect --> Present[Present new suggestion]
    Present --> Accept[User accepts: alternative marked In Progress]
```

### Rejection Handling Prompt

```
The user rejected the suggested task. Understand why and find an alternative.

REJECTED TASK: {task_title}
USER'S REASON: "{rejection_reason}"
REMAINING TASKS: {remaining_tasks_json}
USER CONTEXT: {time} minutes, {mood} mood

REJECTION CATEGORIES:
1. timing - "takes too long", "not enough time"
2. mood_mismatch - "not in the mood", "too tired for that"
3. blocked - "waiting on something", "can't do it yet"
4. already_done - "already did that", "finished already"
5. general - "just not feeling it", vague rejection

ACTIONS BY CATEGORY:
- timing: Suggest shorter task, note time preference
- mood_mismatch: Suggest different work type, avoid this type now
- blocked: Mark as blocked, don't suggest until unblocked
- already_done: Mark as completed, celebrate!
- general: Log rejection, try very different task

OUTPUT (JSON):
{
  "rejection_category": "...",
  "task_update": {
    "rejection_count_increment": 1,
    "rejection_note": "[timestamp] {reason}"
  },
  "alternative_task_id": "..." or null,
  "user_message": "conversational response with {task} if alternative_task_id is non-null"
}
```

When `alternative_task_id` is non-null, `user_message` uses the literal token
`{task}` wherever it refers to the alternative task. The application substitutes
the exact selected title before sending the message.

### Task Status After a Rejection

The rejected task returns to Pending in Notion and stops being the active
task. After a rejection no task is active and the conversation is in
`selection`.

When `alternative_task_id` names a pending task with a title, the reply names
it and the recent-task ledger records it as `suggested`. The alternative stays
Pending: offering a task after a "no" is not the user choosing it. When the
user accepts it on the next turn with a short affirmative ("sure", "ok, that
one"), the chat node marks it In Progress, makes it the active task, and
confirms it by name. Check-ins, breakdown help, and a bare "done" then resolve
against it.

Why this design: the rejection moment carries the highest shame risk, and
initiation happens at acceptance. Turning an offer into a commitment before
the user says yes adds pressure at exactly that moment, and makes later help,
completion, or another rejection act as though the user had picked the task.

### Rejection Response Templates (Shame-Safe)

> **Shame Prevention:** Every rejection response must reinforce that rejecting tasks is helpful, not failure. User gives info about what works. Say so.

| Category | Response Template |
|----------|-------------------|
| timing | "Got it — that one's too long right now. How about {task}?" |
| mood_mismatch | "Fair enough — that tells me what kind of work fits right now. How about {task}?" |
| blocked | "I'll hold off on that one. In the meantime, try {task}?" |
| already_done | "Oh nice, already done! Let me mark that off. Ready for another?" |
| general | "No problem — that helps me learn what works for you. Here's something different: {task}?" |

### Escalation After Multiple Rejections (Shame-Aware)

> **Critical shame protection.** Multiple rejections = highest-risk shame moment. User may feel "broken." Every escalation must explicitly normalize.

```mermaid
flowchart TD
    R1["1st rejection"] --> Try1["Suggest alternative<br/>'No problem — here's something different'"]
    Try1 --> R2["2nd rejection"]
    R2 --> Try2["Very different task + normalize<br/>'Your no's help me learn — trying something else'"]
    Try2 --> R3["3rd rejection"]
    R3 --> Normalize["Explicit normalization<br/>'Sometimes the brain just isn't in task mode.<br/>That's not a failure — it's information.'"]
    Normalize --> Offer["Offer choice: describe mood OR take a break"]
    Offer -->|Describes mood| Targeted["Search with explicit criteria"]
    Offer -->|Break| SafeExit["'I'll be here when you're ready.<br/>No pressure, no judgment.'"]
    Targeted --> R4{4th rejection?}
    R4 -->|Yes| SafeExit
    R4 -->|No| Continue["Continue"]
```

### Emotional Distress Detection

Watch for frustration, shame, or overwhelm signals:

| Signal | Pattern | Response |
|--------|---------|----------|
| Frustration | "ugh", "I can't", short angry messages | "I hear you. Want to take a break, or try something totally different?" |
| Self-blame | "I'm useless", "what's wrong with me" | "Nothing's wrong with you. Brains just work differently with different tasks — that's not a flaw. Want to step away for a bit?" |
| Withdrawal | Increasingly short responses, long pauses | Offer exit ramp: "We can pick this up later. I'll be here." |
| Overwhelm | "too much", "I can't handle this" | "Let's pause. You don't have to do anything right now. The tasks aren't going anywhere." |

**Important:** Never be patronizing. Keep casual tone. Normalization should feel like friend who gets it, not therapist delivering script.


---

See also:
- `docs/ai-prompts/shared.md` — shame-prevention base, base prompt
