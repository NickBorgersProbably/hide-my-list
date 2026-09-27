# Bug 0684: A Completed Task Keeps Getting Deadline Nudges

**PR:** #684

## Bug Story

A task with a deadline carries a series of `kind='deadline'` outbox rows.
Completing the task (through `complete_node`, the shared `_log_finished`
path, or the interaction review's `complete_task` correction) cancelled only
`kind='reminder'` rows, and the worker's pre-send check read the page only
for reminder rows. The remaining deadline nudges kept firing after the task
was Completed, asking the user for a next step on work already done.

## Fix

- `reminders.cancel_pending_nudges(peer, notion_page_id)` marks the page's
  pending/scheduled `kind='deadline'` rows `dead` with
  `last_error='task completed'` and marks the series' active
  `reminder_scheduling_ledger` rows superseded. Every completion write path
  calls it, best-effort. `cancel_pending_for_page` keeps its reminder-only
  semantics.
- The worker's pre-send check reads the page for both kinds: a deadline row
  whose page is already Completed is marked `dead`
  (`last_error='page already completed'`) and not sent. A failed read defers
  the deadline row to `scheduled` (fail-closed); reminder rows still fail open.

## Regression Tests

- `test_completed_task_stops_nudging.py` (real Postgres): a scheduled series,
  a "done" through `complete_node`, then a worker cycle with every row due —
  nothing is sent and every undelivered row is dead. A second test leaves the
  rows in place (as if the cancel failed) and shows the worker's pre-send
  check still keeps a completed task's nudge from going out.
- Call-site kwargs and the Postgres round-trip:
  `tests/integration/test_completion_cancels_nudges.py`; worker unit tests:
  `tests/unit/test_reminder_worker.py`.
- Conversation layer: `tests/e2e/scenarios/test_loop_completed_task_stops_nudging.py`.
