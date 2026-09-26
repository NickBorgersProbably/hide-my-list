# Bug 0681: Rejection Escalation and Distress Handling Missing From Runtime Prompt

**Issue:** #681 (placeholder — rename this directory and reference to the real PR/issue number once this fix's PR is opened)

## Bug Story

`docs/ai-prompts/rejection.md` requires two shame-safety behaviors the
runtime template (`app/prompts/rejection.md.j2`) omits: after the third
consecutive rejection in a session, the reply normalizes and offers a
constrained choice (describe how you're feeling, or take a break) instead of
suggesting another task; and when the user's message carries self-blame or
distress, the reply reframes without judgment and offers to step away
instead of pushing a task. The runtime prompt only listed normalization
copy for the third rejection without instructing the model to stop
suggesting tasks, and its self-blame row dropped the spec's "Want to step
away for a bit?" clause.

State also has no field tracking how many rejections happened in a row this
session — a session's rejections land on different tasks each time (the
user is offered a new alternative after each no), so a single task's Notion
`Rejection Count` property does not track it either.

## Fix

`app/graph/nodes/rejection.py` derives the session's rejection streak from
the recent-task ledger (`_consecutive_rejection_count`): walking the ledger
newest-first, skipping the pending `suggested` alternative, and counting
consecutive `rejected` entries until a `completed`, `added`, `reminded`, or
`nudged` event breaks the streak. The node renders this count (plus the
current rejection) into the prompt as `rejection_streak`.

`app/prompts/rejection.md.j2` renders `REJECTION STREAK: {{ rejection_streak
}}` in the escalation section and instructs the model: at the 3rd rejection
and every one after, do not suggest another task — normalize first, then
offer the constrained choice, and set `alternative_task_id` to null. The
Emotional Distress Detection section carries the same "do not push a
task, offer to step away" instruction and restores the self-blame row's
"Want to step away for a bit?" clause.

## Regression Tests

Test lives in `tests/unit/test_rejection_escalation.py`. It asserts the
rendered template carries the "do not suggest another task" / "constrained
choice" anchors and the self-blame "step away" anchor, exercises
`_consecutive_rejection_count` directly against several ledger shapes, and
checks `rejection_node` renders the derived `REJECTION STREAK` value into
the prompt sent to the model.

- `tests/evals/fixtures/rejection/third_no_normalizes.yaml` and
  `tests/evals/fixtures/rejection/self_blame_gets_exit_ramp.yaml` add judge
  contracts requiring the full spec behavior (no task suggested; the
  constrained choice or step-away offer given instead), which fail against
  the prior prompt and pass against the fixed one.
