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
never contrasts the report against the list, offering the context task by name
when there is one, and records the question in `pending_clarification`.
`complete_node.unlisted_report` logs booleans and counts only.

A null match over the widened list without the flag still lets context
resolve, so "done :) feeling good" keeps completing the active task
(`bug_0664_complete_clarification_loop`).

## Regression Tests

- `test_unlisted_report.py` pins the node behavior with a stubbed model.
- The model-behavior test is `tests/evals/fixtures/complete/unlisted_report_asks.yaml`.
- The intake `already_done` handoff, which delegates to `complete_node`, is
  covered in `tests/integration/test_intake.py`.
