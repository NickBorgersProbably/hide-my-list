# Bug 0677: Intake Confirmation Dumps Labels, a Numbered Plan, and the Nudge Schedule

**PR:** #677

## Bug Story

The user said "I need to <task> this week". The confirmation read like a
report: the work-type label, a time estimate, "Here's your plan: 1) … 2) …
3) …", and then every deadline nudge the planner had assigned ("I'll ping you
Thu 5pm, Mon 5pm, Wed 5pm, and Thu 1pm"). The intake prompt asked for labels
and a numbered plan, and `format_reminder_summary` listed every slot.

## Fix

The confirmation is one sentence naming `{task}` plus the stated deadline or
reminder time, optionally followed by `First step: <step>.` No work type,
estimate, numbered plan, or step count. Sub-tasks and `inline_steps` are
still generated and stored. `format_reminder_summary` names the earliest slot
only (`First nudge <local time>.`). The fallback confirmation when the model
omits one is `Got it — {task}.`

## Regression Tests

- `test_intake_plan_dump.py` pins the prompt contract (no plan/label/step-count
  instructions, a `First step:` example, the never-list), the one-slot summary,
  and the node's label-free fallback.
- The model-behavior test lives in `tests/evals/fixtures/intake/confirmation_one_sentence.yaml`.
- The full-graph turn (including the appended nudge sentence) is
  `tests/e2e/scenarios/test_loop_intake_reply_shape.py`.
