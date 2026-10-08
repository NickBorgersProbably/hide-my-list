# Bug 0687: A State Report Is Not Matched as a Completion

**PR:** #687

## Bug Story

A user said "Deep clean is scheduled!" (placeholder wording). The only open
task was "Schedule a deep clean for the trip". The `complete_title_match` call
(cheap tier, reasoning off) returned no match: it read "is scheduled" as a
future state. The standalone match instructions only said what is NOT a match
(a task the user "is about to start"), and nothing said that a report of the
resulting state means the task whose action produces that state is done.

After the failed match the node fell back to the task it had suggested the
turn before and asked "Nice — which task was it: <one title>?". With a single
option that is a choice between one thing.

Measured against the live proxy (cheap tier, `think: false`, 10 calls each):

| Message vs candidate | Before | After |
|----------------------|--------|-------|
| "Deep clean is scheduled!" vs "Schedule a deep clean for the trip" (want match) | 0/10 | 10/10 |
| "done, now I need to call mom" vs "Call mom" (want no match) | 0/10 | 0/10 |
| "Did the form submit" vs "Submit the form" (want no match) | 0/10 | 0/10 |

## Fix

- The standalone match prompt in `app/graph/nodes/complete.py`
  (`_build_completion_match_prompt`) says a report of the resulting state
  ("X is scheduled", "the form is submitted", "the appointment is booked",
  "tickets are bought") asserts completion of the candidate whose action
  produces it, and a question about the state is not a match. The answer
  framing (a reply to "which one?") is unchanged.
- `_clarification_body` asks "Nice — was it <title>?" (first ask) and "Just
  checking — was it <title>?" (re-ask) when exactly one option comes from
  context. Multi-option and shortlist wording are unchanged.
- Spec: `docs/ai-prompts/shared.md`, Cross-Session Reply Resolution and
  Pending Clarification.

## Regression Tests

- `test_state_report_completion.py`: the prompt the node sends for a state
  report carries the rule; a rejected match with one context option asks "was
  it <title>?" and completes nothing; the re-ask wording differs; two
  context options keep the choice wording.
- Unit: `tests/unit/test_complete_task_reference.py`; integration:
  `tests/integration/test_intent_nodes.py`.
- Eval (model behavior): `tests/evals/fixtures/complete/state_report_is_a_completion.yaml`
  and the guard `tests/evals/fixtures/complete/state_question_is_not_a_completion.yaml`.
