# Bug 0687: Chat Denies History It Cannot See, and Offers the List

**PR:** #687

## Bug Story

A user asked "What deadline were you nudging me for?" (CHAT). The deadline
nudges had gone out more than 10 days earlier. The recent-task ledger keeps 7
days and 8 entries, so the chat prompt's `### Recent Tasks` block did not show
them, and the model replied "I haven't nudged you about any deadlines yet!".
That was false, and it contradicted messages the user had received.

In the same session chat twice closed with "Want to see your other tasks?".
The user never sees the full task list (`docs/ai-prompts/shared.md`,
CONSTRAINTS), so no module can honor that offer: accepting it routes to
GET_TASK and yields one suggestion.

## Fix

- `app/prompts/chat.md.j2` has a `### No record is not never` section: when
  the user refers to a reminder, nudge, or task that Recent Tasks does not
  show, chat says it has no record of a recent one, never denies it
  happened, and offers one forward step.
- The Response Guidelines forbid offering to show, list, or enumerate tasks.
  When a forward step fits it is one suggestion or adding something new.
- The spec text lives in the Recent Task Ledger section of
  `docs/ai-prompts/shared.md`.

## Regression Tests

- `test_chat_denies_unseen_history.py`: with an empty ledger, the system
  prompt `chat_node` sends to the model carries the no-record rule and the
  never-list guideline, and the model is called on the medium tier with
  caller `chat`.
- Unit: `tests/unit/test_chat_prompt_structure.py`.
- Eval (model behavior): `tests/evals/fixtures/chat/no_record_is_not_denial.yaml`,
  `tests/evals/fixtures/chat/never_offers_the_list.yaml`.
