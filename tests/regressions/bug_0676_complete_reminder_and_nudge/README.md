# Bug 0676: COMPLETE Cannot Find Reminders, Has No Anchor, Names Nothing

**PR:** #676

## Bug Story

A user asked for a reminder and said "Done!" a minute later. The agent asked
which task they meant. Reminder pages were filtered out of every COMPLETE
lookup, and nothing recorded the page intake had just created, so the question
had no right answer. Typing the title back did not help. When a "done" did
resolve, the celebration named no task, and the user had to ask what had been
completed. A "done" after a deadline nudge rewarded the user and left the task
open: COMPLETE skipped the Notion write for every `recent_outbound` target,
treating the nudge like a reminder page that delivery had already completed.

## Fix

- COMPLETE matches against open reminder pages as well as tasks. Finishing a
  reminder before it fires writes Completed and cancels its pending outbox rows
  (`reminders.cancel_pending_reminders`). A cancellation that fails twice
  still completes and raises a `reminder_cancel_failed` ops alert; the
  delivery worker skips any reminder whose page is already Completed.
- A bare "done" anchors to the recent-task ledger. Two different tasks touched
  within 15 minutes of each other are ambiguous, and the agent names both.
- An answer to "which task?" that types a title back nearly verbatim resolves
  without a model call. Standalone messages still need the model.
- The celebration body is `{task} — done. <reward text>` and the draft carries
  `notion_page_title`, so `send_node` names the task.
- Only a delivered reminder (a `recent_outbound` row whose `reminder_type` is
  not `deadline`) skips the Notion write. Deadline nudges name their task.
- Rejection records the declined page as `rejected` and the offered
  alternative as `suggested`, so a bare "done" after a rejection anchors to the
  alternative.

## Regression Tests

- `test_complete_reminder_and_nudge.py` covers each shape above, including a
  `rejection_node` → `complete_node` chain. Two tests use real Postgres: the
  reminder's outbox row is dead after the completion, and a delivered nudge
  is loaded (`reminders.load_recent_outbound`) and resolved
  (`reminders.resolve_recent_outbound`) through the tools layer. The
  cancellation call is bound against the tool's signature, and the retry and
  alert paths are pinned.

Related coverage: `tests/integration/test_outbox.py::test_cancel_pending_for_page_kills_only_pending_reminder_rows`,
`tests/unit/test_complete_task_reference.py`,
`tests/unit/test_reminder_worker.py` (the pre-send check), and the e2e loops
`tests/e2e/scenarios/test_loop_*.py`.
