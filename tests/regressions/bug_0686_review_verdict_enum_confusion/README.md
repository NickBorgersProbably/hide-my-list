# Bug 0686: The Interaction Review Writes an Action Name Into `verdict`

**PR:** #686

## Bug Story

The post-send interaction review returns a JSON verdict with two enum fields:
`verdict` (`ok` | `correct`) and `action` (`none` | `complete_task` |
`send_only`). On the turn it exists for — the user said "Done!" and the turn
asked which task — the model sometimes answered
`{"verdict": "complete_task", "action": "complete_task", ...}`. `parse_verdict`
rejected it as `unknown_verdict`, the row was stored as `error`, and the
missed completion the review had correctly found was never repaired. The
e2e loop scenario for the review failed on roughly one CI run in several.

## Fix

- `parse_verdict` normalizes one mix-up: a `verdict` that is itself an action
  name and equals `action` exactly — `none`/`none` maps to `ok`,
  `complete_task`/`complete_task` and `send_only`/`send_only` map to `correct`
  — logged as `interaction_review.verdict_normalized` with enums only. Two
  different action names, an invalid action, and every page rule stay rejected.
- The prompt (`app/prompts/interaction_review.md.j2`) and its spec
  (`docs/ai-prompts/interaction-review.md`) name each enum's exact values,
  say what each field means, and show one example of each verdict.
- The eval runner reports a verdict that needed normalizing as
  `NORMALIZED_VERDICT:` so the eval layer keeps scoring the prompt, not the
  parser.

## Regression Tests

- `test_review_verdict_enum_confusion.py`: the observed shape is accepted as
  `correct`/`complete_task`; contradictory shapes stay rejected.
- Unit: `tests/unit/test_interaction_review.py`; integration (real Postgres):
  `tests/integration/test_interaction_review.py::test_an_action_name_in_verdict_still_completes_the_reminder`.
- Eval: `tests/evals/fixtures/interaction_review/verdict_is_correct_not_action_name.yaml`
  and `verdict_is_correct_for_a_task.yaml`.
- Conversation layer: `tests/e2e/scenarios/test_loop_interaction_review.py`.
