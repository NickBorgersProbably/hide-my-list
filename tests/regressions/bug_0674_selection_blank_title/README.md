# Bug 0674: Selection Suggests a Task With a Blank Title

**PR:** #674

## Bug Story

The user asked for something to knock out right now. The selection model
returned a `selected_task_id` that was not in the task list the node had scored.
`selection_node` still treated it as a selection: it wrote In Progress to that
unknown page and built `ActiveTask(title="")`. The draft carried no
`notion_page_title`, so `send_node` had nothing to substitute, and the user
received "how about this focus task?" with no task named. The empty-titled
active task then leaked into the next turn's COMPLETE and reward paths.

## Fix

A selection counts only when the id names a scored task with a non-empty title.
Any other id, or a blank-titled page, is treated as no selection: there is no
In Progress write, no `active_task`, and no ledger entry, and the user gets a
neutral retry reply ("Couldn't land on one just now — ask me again in a sec?")
rather than an offer to add a task. The node logs
`selection_node.unknown_page_id` with booleans and a candidate count only; the
model-supplied id is free text and is never logged. A body that
writes `{task}` with a `null` selection gets the no-match reply. The spec lives in
`docs/ai-prompts/selection.md` under "Unknown Selection Guard".

## Regression Tests

- `test_selection_blank_title.py` replays the reported shape (an unknown id
  with an attribute-only body) and a listed page with a blank title. It asserts
  no Notion write, no active task, no ledger entry, a delivered body with no
  unfilled token, the retry reply, and a log event that never carries the raw
  id.

Related coverage: `tests/integration/test_intent_nodes.py::test_selection_node_unknown_page_id_is_not_suggested`.
