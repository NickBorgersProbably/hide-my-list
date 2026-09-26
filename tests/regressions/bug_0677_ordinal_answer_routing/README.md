# Bug 0677: A Positional Answer to a Clarification Is Classified ADD_TASK

**PR:** #677

## Bug Story

The completion module asked which of two named tasks the user finished. The
user answered "the first one". The classifier prompt carries a rule that sends
"it's new, log it" replies to ADD_TASK while a clarification is open, and the
cheap model applied it to the positional answer as well. ADD_TASK dropped the
open clarification, intake handed the turn back to the completion module
without the named options, and the agent asked the same question again.

## Fix

Two layers. `classify_intent` checks a live clarification before calling the
model: a whole-message positional or affirmative reply ("the first one", "number
2", "that one", "yes") resolves to COMPLETE with the clarification kept, and
logs `classify_intent.clarification_option_reference`; a bare negative ("no",
"nope", "neither") clears the clarification and sends "Got it, leaving that
open." The match is against the whole normalized message, so "no it's new, just
log it" still goes to the model. The classifier prompt rule is tightened: ADD_TASK during a
clarification needs both "it's new" and "log/add/track it", and a reply that
picks an option is COMPLETE, with "the first one" / "the second one" examples.

## Regression Tests

- `test_ordinal_answer_routing.py` pins the guard (no model call, COMPLETE,
  clarification kept), its whole-message boundary, and the prompt examples.
- `tests/unit/test_routing_option_reference.py` covers every positional form,
  the no-clarification path, and expired/malformed records.
- The prompt backstop is `tests/evals/fixtures/classify_intent/ordinal_answer_during_clarification.yaml`.
- The two-turn conversation is
  `tests/e2e/scenarios/test_complete_clarification.py::test_a_positional_answer_resolves_the_option_it_points_at`.
