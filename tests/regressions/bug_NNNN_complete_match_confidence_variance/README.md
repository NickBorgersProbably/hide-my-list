# Bug NNNN: A Named Completion Asks "Which Task?" When the Match Call Misfires

**PR:** #NNNN

## Bug Story

COMPLETE resolves a task named in a standalone message by asking the
cheap-tier model to confirm a candidate at `match_confidence >= 0.90`. With
reasoning off for that caller, roughly one e2e scenario per CI run asked
"which task did you mean?" about a task the message had just named ("finished
washing the dishes", a one-synonym paraphrase, completing the alternative
just offered), a different scenario each time. Measured against the live
model: the confidence on these shapes is 1.0 on nearly every call (340 of 342
matches; two at 0.9, which passes). The misfire is the page id — the model
copies a 36-character UUID back with characters dropped on about one call in
eighty, the id matches no candidate, and `parse_match_response` reads it as a
null match.

## Fix

- The match prompt carries short aliases (`t1`, `t2`, …) instead of page ids,
  and `_parse_aliased_match` maps the answer back. The model never sees a
  page id, so it has none to mistype. A mangled or unknown id is still no
  match; the node never guesses from a partial id.
- `_standalone_report_names` resolves a standalone report without a model call
  when the whole message is one open task's title plus report filler: every
  title word present (after a crude symmetric stem), every other word on the
  `_REPORT_FILLER` allowlist, a completion claim (a claim word or a past-tense
  title word), not a question, and exactly one open task qualifying. Any
  other word — "need", "now", "still", a second task — sends the message to
  the model as before, so "done, now I need to call mom" still does not
  complete "Call mom".
- A shortlist match the model scored under the bar logs
  `complete_node.title_match_rejected` with `match_confidence` as a number,
  and the e2e debug dump prints that key, so a CI failure shows the score.

## Regression Tests

- `test_complete_match_confidence_variance.py`: the title-shaped e2e reports
  complete with the model pinned to the observed truncated-id answer (and the
  model never called); a paraphrase resolves through an alias with no page id
  in the prompt; a truncated id is still refused; "done, now I need to call
  mom" still calls the model and writes nothing.
- Unit: `tests/unit/test_complete_task_reference.py` (the shortcut's accept
  and reject sets, the alias round trip); integration:
  `tests/integration/test_intent_nodes.py`.
- Eval: `tests/evals/fixtures/complete/paraphrase_uuid_page_ids.yaml`.
- Conversation layer: `tests/e2e/scenarios/test_complete_by_name.py`,
  `test_complete_clarification.py`, `test_loop_suggest_reject_complete_alternative.py`.
