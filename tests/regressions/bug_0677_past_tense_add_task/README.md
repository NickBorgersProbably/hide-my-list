# Bug 0677: A Past-Tense Report Is Classified ADD_TASK

**PR:** #677

## Bug Story

The user reported something they had already done that was never on the list
("I also <did X>!"). The classifier had no past-tense example, so it returned
ADD_TASK, and intake tried to save a finished thing as a new task; in
production the turn then errored. Nothing on the ADD_TASK path could notice
that the message reported a completion.

## Fix

Two layers. The classifier prompt carries the rule "a past-tense report of
something the user did is COMPLETE even when it names something not on the
list" with examples ("I also paid the gas bill!", "finished that one too"),
plus the examples that keep neighbouring messages where they belong ("What
task?" and "sure" after a suggestion are CHAT; "no it's new, just log it"
while a completion clarification is open is ADD_TASK). As a backstop, the
intake model can return `action: "already_done"`; intake then saves nothing,
logs `intake_node.already_done_handoff`, and returns `complete_node(state)`.

When the user then answers "which task was that?" with "no it's new, just log
it", the thing is still already done. Intake returns a save marked
`"already_finished": true`: the page is created with Status Completed (or, when
the title matches an open task at the dedup threshold, that task is completed
instead), `maybe_reward` runs, the ledger records `completed`, and the reply is
the completion celebration naming `{task}`. An accomplishment never becomes
another open obligation.

## Regression Tests

- `test_past_tense_add_task.py` pins the classifier rule and examples and the
  intake handoff (no page created, `complete_node` awaited with the same state).
- The model-behavior test lives in `tests/evals/fixtures/classify_intent/past_tense_report.yaml`;
  the intake backstop is `tests/evals/fixtures/intake/already_done_handoff.yaml`.
- `test_past_tense_add_task.py` also pins the intake prompt's
  `already_finished` rule and asserts that a save marked `already_finished`
  creates a Completed page and replies with the celebration.
- The logging turn's model behavior is `tests/evals/fixtures/intake/log_new_from_history.yaml`.
- The two-turn conversation (report, then "no it's new, just log it") is
  `tests/e2e/scenarios/test_loop_past_tense_then_log_new.py`; turn 2 asserts
  the new page is Completed and the reply names it.
