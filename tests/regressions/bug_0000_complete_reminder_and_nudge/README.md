# Bug 0000: COMPLETE Cannot Find Reminders, Has No Anchor, Names Nothing

**PR:** #0000

## Bug Story

A user asked for a reminder and said "Done!" a minute later. The agent asked
which task they meant. Reminder pages were filtered out of every COMPLETE
lookup, and nothing recorded the page intake had just created, so the question
had no right answer. Typing the title back did not help. When a "done" did
resolve, the celebration named no task, and the user had to ask what had been
completed. A "done" after a deadline nudge rewarded the user and left the task
open: the worker recorded every delivery as `reminder_type='reminder'`, so the
nudge looked like a reminder page that delivery had already completed. A
past-tense report of something that was never on the list ended in the same
question.

## Fix

- COMPLETE matches against open reminder pages as well as tasks. Finishing a
  reminder before it fires writes Completed and cancels its pending outbox rows
  (`reminders.cancel_pending_for_page`).
- A bare "done" anchors to the recent-task ledger. Two different tasks touched
  within 15 minutes of each other are ambiguous, and the agent names both.
- An answer to "which task?" that types a title back nearly verbatim resolves
  without a model call. Standalone messages still need the model.
- The celebration body is `{task} — done. <reward text>` and the draft carries
  `notion_page_title`, so `send_node` names the task.
- The worker records the outbox kind as `reminder_type`. Only a delivered
  reminder skips the Notion write. Deadline nudges name their task.
- A confident report of finishing something on none of the open tasks is
  logged as a new Completed task, and the reply says it was not on the list.

## Regression Tests

- `test_complete_reminder_and_nudge.py` covers each shape above. The first test
  uses real Postgres to prove the reminder's outbox row is dead after the
  completion.

Related coverage: `tests/integration/test_outbox.py::test_cancel_pending_for_page_kills_only_pending_reminder_rows`,
`tests/unit/test_complete_task_reference.py`, and the e2e loops
`tests/e2e/scenarios/test_loop_*.py`.
