# Bug 0686: A Named Completion Asks "Which Task?" When the Match Call Misfires

**PR:** #686

## Bug Story

COMPLETE resolves a task named in a standalone message by asking the
cheap-tier model to confirm a candidate at `match_confidence >= 0.90`. With
reasoning off for that caller, roughly one e2e scenario per CI run asked
"which task did you mean?" about a task the message had just named ("finished
washing the dishes", a one-synonym paraphrase, completing the alternative
just offered), a different scenario each time. Measured against the live
model: the confidence on these shapes is 1.0 on nearly every call (364 of 366
matches; two at 0.9, which passes). The misfire is the page id — the model
copies a 36-character UUID back with characters dropped in 2 of 182 paraphrase
calls, the id matches no candidate, and `parse_match_response` reads it as a
null match.

## Fix

- The match prompt carries short aliases (`t1`, `t2`, …) instead of page ids,
  and `_parse_aliased_match` maps the answer back. The model never sees a
  page id, so it has nothing to mistype. A mangled or unknown alias is still
  no match; the node never guesses from a partial answer.
- A shortlist match the model scored under the bar logs
  `complete_node.title_match_rejected` with `match_confidence` as a number,
  and the e2e debug dump prints that key, so a CI failure shows the score.

## Regression Tests

- `test_complete_match_confidence_variance.py`: the title-shaped e2e reports
  complete via the alias-backed model path with no page id in the prompt; a
  paraphrase resolves through an alias; a truncated id is still refused; "done,
  now I need to call mom" still calls the model and writes nothing; a
  punctuationless question ("Did the form submit") goes to the model without
  completing the task.
- Unit: `tests/unit/test_complete_task_reference.py` (alias round trip);
  integration: `tests/integration/test_intent_nodes.py`.
- Eval: `tests/evals/fixtures/complete/paraphrase_uuid_page_ids.yaml`.
- Conversation layer: `tests/e2e/scenarios/test_complete_by_name.py`,
  `test_complete_clarification.py`, `test_loop_suggest_reject_complete_alternative.py`.
