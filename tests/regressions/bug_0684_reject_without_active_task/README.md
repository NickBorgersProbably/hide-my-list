# Bug 0684: REJECT With Nothing Active Sends a Literal `{task}`

**PR:** #684

## Bug Story

A user with no suggestion on the table said "never mind, I'll check later".
The classifier routed it to REJECT. `rejection_node` ran the rejection prompt
with no active task, the model wrote one of the prompt's alternative
templates ("How about {task}?") without a listed alternative behind it, and
`render_task_token` left the token in place because there was no title to
substitute. `send_node` only enforced naming for drafts carrying
`notion_page_title`, so the literal `{task}` reached the user.

## Fix

- With no active task and no titled ledger suggestion from the last 24 hours,
  `rejection_node` runs no prompt, reads and writes nothing in Notion, records
  nothing in the ledger, and replies with a fixed acknowledgement that names
  nothing (`NOTHING_ACTIVE_BODY`).
- With a task active but no listed, titled alternative, the sentence carrying
  the token is dropped; when nothing is left, the no-alternative reply goes
  out. The draft carries no alternative page id.
- `send_node` enforces the inverse invariant for every draft: a `{task}`
  token with no title behind it is logged `send_node.orphan_task_token` and
  its sentence is dropped; if nothing remains, the neutral fallback goes out.

## Regression Tests

- `test_reject_without_active_task.py` drives the reported message through
  `rejection_node` and `send_node` and asserts the delivered body carries no
  token, then covers the model-writes-a-token-with-no-alternative path.
- Node-level coverage: `tests/integration/test_rejection_orphan_token.py`;
  send guard: `tests/unit/test_send_node_task_title.py`.
- Conversation layer: `tests/e2e/scenarios/test_loop_reject_with_nothing_active.py`.
