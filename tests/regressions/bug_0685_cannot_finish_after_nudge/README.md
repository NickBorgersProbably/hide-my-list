# Bug 0685: "I Can't Finish That" After A Deadline Nudge Has No Task

**PR:** #685

## Bug Story

The worker delivered a deadline nudge for an open task. The user answered it
with "I can't finish that today". `cannot_finish_node` read only the
checkpointed `active_task`, which a nudge never sets, so the prompt said
CURRENT TASK: "your task" and the reply was a generic progress question that
named nothing. Anything the user said next about their progress had no page
to attach to. `hydrate_context` had already merged the delivery into the
recent-task ledger as a `nudged` entry with the page id and stored title; the
node never looked. The long e2e loops (`test_loop_bad_days.py`,
`test_loop_week_in_the_life.py`) found it.

The node also wrote nothing to Notion at all: `docs/ai-prompts/cannot-finish.md`
has the model return `remaining_sub_tasks` once progress is known, and the
node dropped them.

## Fix

- With no `active_task`, `cannot_finish_node` anchors to the newest recent-task
  ledger entry whose latest event is `nudged`, `reminded`, or `suggested`,
  carries a title, and is at most 24 h old (`ledger_anchor`, mirroring
  `need_help.py`'s ledger fallback). It reads that page's time estimate, work
  type, and energy from Notion, fail-soft.
- The draft carries the page id and stored title, so `send_node` names the
  task.
- On `phase: analyze_remaining`, each remaining sub-task (at most six, titled)
  is created as a hidden child of the resolved page (`Parent Task`,
  `Sequence`). No resolved page means no writes.
- `docs/ai-prompts/cannot-finish.md` (Task Resolution and Notion Writes)
  specifies the resolution order and the writes. The runtime prompt is
  unchanged: it already renders CURRENT TASK, which now carries the title.

## Regression Test

`test_cannot_finish_after_nudge.py` runs the node with a mocked model on a
state carrying only a fresh `nudged` ledger entry. It fails on the old code:
the prompt said "your task", the draft had no page or title, and the model's
sub-tasks were never written.

Related layers: `tests/integration/test_reminder_reschedule.py`,
`tests/evals/fixtures/cannot_finish/anchors_to_nudged_reminder.yaml`,
`tests/e2e/scenarios/test_loop_reschedule_reminder.py`.
