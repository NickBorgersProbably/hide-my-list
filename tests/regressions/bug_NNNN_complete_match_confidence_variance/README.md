# Bug NNNN: A Named Completion Asks "Which Task?" on a Low Model Score

**PR:** #NNNN

## Bug Story

COMPLETE resolves a task named in a standalone message by asking the
cheap-tier model to confirm a candidate at `match_confidence >= 0.90`. With
reasoning off for that caller, the score on unambiguous reports — "finished
washing the dishes" against "Wash the dishes", "ok, replied to the school
email" against "Reply to the school email" — varied run to run and sometimes
landed under the bar, so the node asked which task the user meant about the
task they had just named. Roughly one e2e scenario per CI run failed this way,
a different one each time.

## Fix

- `_standalone_report_names` in `app/graph/nodes/complete.py` resolves a
  standalone report without a model call when the whole message is one open
  task's title plus report filler: every title word present (after a crude
  symmetric stem), every other word on the `_REPORT_FILLER` allowlist, a
  completion claim (a claim word or a past-tense title word), not a question,
  and exactly one open task qualifying. Any other word — "need", "now",
  "still", a second task — sends the message to the model as before, so
  "done, now I need to call mom" still does not complete "Call mom".
- A shortlist match the model scored under the bar logs
  `complete_node.title_match_rejected` with `match_confidence` as a number,
  and the e2e debug dump prints that key, so a CI failure shows the score.

## Regression Tests

- `test_complete_match_confidence_variance.py`: the three e2e shapes complete
  with the model pinned to the sub-threshold answer it gave on failing runs
  (and the model never called); "done, now I need to call mom" still calls
  the model and writes nothing.
- Unit: `tests/unit/test_complete_task_reference.py` (the shortcut's accept
  and reject sets); integration: `tests/integration/test_intent_nodes.py`.
- Conversation layer: `tests/e2e/scenarios/test_complete_by_name.py`,
  `test_complete_clarification.py`, `test_loop_suggest_reject_complete_alternative.py`.
