# Bug 0685: Changing A Reminder's Time Creates A Second Reminder

**PR:** #685

## Bug Story

A user asked for a reminder at 5pm, then a minute later said "actually make it
6pm". Intake saved the follow-up as a brand-new reminder: a second Notion page
and a second outbox row. The 5pm original stayed pending and fired anyway, so
the user got two reminders for one thing, one of them at the time they had
just asked to change. The same happened after delivery ("push that to 8"):
the delivered page stayed Completed and a new page appeared.

The intake prompt had a reschedule section, but it read a
`recent_outbound_context` input the node never passed, so the model had no
reminder to move and no way to say it was moving one. Reminders also skip
duplicate detection, so nothing downstream caught it. The long e2e loops
(`test_loop_reminder_lifecycle.py`) found it; they asserted only that some
reminder sat at the new time.

## Fix

- Intake shows the model the recent-task ledger's fresh reminders (just set or
  just delivered, last 24 h, confirmed as reminder pages in Notion) under
  labels `R1`, `R2`, ... — never page ids
  (`app/graph/nodes/_reminder_reschedule.py`).
- A time-only follow-up returns `"reschedule_of": "R1"`. The code accepts only
  a label it showed. A valid label moves that page: `Remind At` gets the new
  time and the page is reopened (`Status` Pending, `Reminder Status` pending),
  because the worker skips a Completed page's row at delivery.
- `reminders.reschedule_for_page` swaps the outbox in one transaction: the
  page's waiting reminder row goes `dead` with `last_error='rescheduled by
  user'` and one new pending row waits for the new time. Deadline rows and
  other peers' rows are untouched.
- Any other `reschedule_of` (null, unknown label) creates a reminder as
  before. A move with no usable time asks for one and writes nothing.
- Prompt and spec (`app/prompts/intake.md.j2`, `docs/ai-prompts/intake.md`,
  MOVING AN EXISTING REMINDER) replace the dead reschedule section.

## Regression Test

`test_reschedule_does_not_duplicate.py` drives two intake turns with a mocked
model and the in-memory Notion, feeding turn 1's ledger into turn 2 the way
the checkpoint does. It fails on the old code: turn 2 created a second
reminder page and left the 5pm row pending. Needs Postgres (`DATABASE_URL`).

Related layers: `tests/integration/test_reminder_reschedule.py`,
`tests/evals/fixtures/intake/reschedule_existing_reminder.yaml`,
`tests/e2e/scenarios/test_loop_reschedule_reminder.py`.
