# Bug 0677: A Report of Something Unlisted Completes the Context Task

**PR:** #677

## Bug Story

The classifier sends a past-tense report of something the user did to COMPLETE,
even when it names nothing on the list ("I also paid the gas bill!"). In
`complete_node` the message has residue the shortlist cannot place, so the
candidate set widens to the whole open list and the model rejects every
candidate. A null match over the widened list is read as "I could not tell",
which lets context resolve — so the live active task was marked Completed and
rewarded on behalf of a different thing the user reported.

## Fix

The standalone match prompt also returns `names_unlisted_task`: true when the
message clearly reports finishing a specific, concrete task that matches none
of the candidates. A null match with that flag set — over the scored shortlist
or the widened list alike — never falls through to the ledger, the recent
delivery, or the active task. The node asks instead, with positive copy that
never contrasts the report against the list, and records the question in
`pending_clarification` as an `unlisted_report` clarification.
`complete_node.unlisted_report` logs booleans and counts only.

Every form of the question is a yes/no choice where one is possible, so the
user never has to recall and retype what they just reported. The match prompt
also returns `unlisted_task_title`, a short title for the report; it is kept
only when it shares a task-naming word with the message.

- With a context task: "Nice one! Did you mean {task}?". "yes" (or "that
  one") completes that task. "no" offers to log the report — "Got it. Want me
  to log '<title>' as done?" — when a title was kept, and otherwise leaves
  everything open.
- With no context task and a title: "Nice one! Want me to log '<title>' as
  done?". "yes" creates the page Completed (or completes the open task it
  duplicates), rewards it, and celebrates it by name through the shared
  `app/graph/nodes/_log_finished.py`; "no" leaves everything open.
- With neither: "Nice one! I've left your list as it is.".

An answer to any of these never falls back to the ledger, the recent delivery,
or the active task.

An empty open list is no exception: with at least two task-naming words
left after the completion words, the node still asks the model (with no
candidates) whether the message names a concrete finished task, so the report
gets the unlisted-task acknowledgment rather than the generic "which task?" question.

A null match over the widened list without the flag still lets context
resolve, so "done :) feeling good" keeps completing the active task
(`bug_0664_complete_clarification_loop`).

## Regression Tests

- `test_unlisted_report.py` pins the node behavior with a stubbed model,
  including the empty-list case, the title grounding, the log stage, and an
  unmatched answer that must not complete the context task.
- `tests/integration/test_unlisted_report_flow.py` drives report → "no" →
  "yes" and report → "yes" through `classify_intent` and `complete_node`.
- The model-behavior tests are `tests/evals/fixtures/complete/unlisted_report_asks.yaml`
  and `tests/evals/fixtures/complete/unlisted_report_log_stage.yaml`.
- The intake `already_done` handoff, which delegates to `complete_node`, is
  covered in `tests/integration/test_intake.py`.
