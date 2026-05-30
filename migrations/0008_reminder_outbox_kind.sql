-- Discriminator on reminder_outbox so the worker can distinguish wall-clock
-- reminders (which complete the user's Notion task on delivery) from
-- deadline-driven series reminders (which must NOT complete the task — the
-- user still has work to do; the deadline-series ping is just a nudge).
--
-- Without this column, deadline reminders set notion_page_id = task page id,
-- and the worker calls notion.complete_reminder(notion_page_id, "sent") after
-- every successful delivery, which would silently mark the task Completed in
-- Notion on the FIRST deadline ping.
--
-- Design:
--   kind='reminder'  — existing wall-clock reminder behavior. Worker calls
--                      complete_reminder on the Notion page after delivery.
--                      Default for legacy rows so existing behavior is preserved.
--   kind='deadline'  — deadline-series milestone reminder. Worker skips
--                      complete_reminder (task remains in its current state).
--
-- All existing rows default to 'reminder', preserving legacy semantics.
-- New deadline-series rows MUST set kind='deadline' explicitly (enforced via
-- CHECK constraint and propagated through the reminders.enqueue() helper).

BEGIN;

ALTER TABLE reminder_outbox
  ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'reminder';

-- Use a DO block so the migration is idempotent across reruns.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'reminder_outbox_kind_check'
  ) THEN
    ALTER TABLE reminder_outbox
      ADD CONSTRAINT reminder_outbox_kind_check
      CHECK (kind IN ('reminder', 'deadline'));
  END IF;
END $$;

COMMIT;
