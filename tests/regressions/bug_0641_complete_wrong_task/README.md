# Bug 0641: COMPLETE closed a task the user never touched

**Issue:** #641

## Bug Story

A user replied to a delivered reminder. The app marked a **different,
unrelated task** as Completed in Notion, rewarded them for it, and left the
reminder they were actually answering unresolved. Nothing errored; the reply
was classified `COMPLETE` correctly. The failure was entirely in *which* task
the node acted on.

`complete_node` read the target from `state["active_task"]` and nothing else.
That entry was a leftover from a selection turn hours earlier — `active_task`
is cleared only by completion and rejection, so a task that is offered and then
simply not acted on stays "active" in the checkpoint forever, remaining a valid
completion target indefinitely.

Meanwhile the reminder worker had written a `recent_outbound` row recording
which page was awaiting a reply. Three separate places describe reading that
row as the intended behavior — `docs/task-lifecycle.md`,
`docs/user-interactions.md`, and the `app/graph/state.py` module docstring,
which states it as fact ("read by graph nodes at turn start"). It was never
implemented. The table had exactly one reader in the whole application:

```
$ grep -rn "FROM recent_outbound" app/ --include=*.py
app/scheduler/jobs.py:182:  "DELETE FROM recent_outbound WHERE expires_at < %s",
```

The producer, schema, and retention job were all built. The consumer was not.
So a terse "done" did not fail closed and ask what the user meant — it fell
through to whatever was in the checkpoint.

The logs showed it plainly: the only Notion write in the turn was a `PATCH` to
page B, while the reminder the user was answering (page A) appeared nowhere in
the turn, and its `recent_outbound` row still read `awaiting_reply = true` a
day later.

**Bug class:** a documented contract with a built producer and no consumer,
combined with a fallback that fails *open* onto unbounded stale state. Adjacent
to bug class 6 (dead-code wiring), but the reachability lint cannot catch it —
the dead thing is a table, not a function.

### Why it stayed invisible

Every layer reported success. `update_status` returned 200 — the API cannot
know the page was the wrong one. The reward delivered normally. The
`complete_node.done` log recorded the page it acted on, with nothing to compare
it against.

What surfaced it was a *secondary* symptom: page B happened to have a blank
title, so the reward image was suppressed and the user got a text fallback
(#632 / #638). Had page B carried a normal title, this would have produced an
ordinary-looking celebration and left no user-visible trace at all.

## Fix

- `app/tools/recent_outbound.py` (new) — the missing reader.
  `load_awaiting_reply()` returns the newest unresolved, unexpired row for a
  peer; `clear_awaiting_reply()` resolves it. Both fail soft.
- `app/graph/nodes/complete.py` — `_resolve_target()` picks between the
  reminder and the `active_task` by recency. The reminder branch skips the
  Notion write (the worker already Completed that page at delivery time) and
  clears the matched row. With no usable candidate the node **asks** which task
  the user finished instead of completing whatever is in the checkpoint.
- `app/graph/state.py` / `app/graph/nodes/selection.py` — `ActiveTask` gains
  `selected_at`, stamped at selection. Entries older than `_ACTIVE_TASK_TTL`
  (24h, mirroring the `recent_outbound` expiry) are not completion targets.
  An entry with no `selected_at` is treated as stale.
- `complete_node.target_resolved` logs the decision and the candidates that
  were considered, so a wrong-target completion is visible in the logs rather
  than indistinguishable from a right one.

## Regression Tests

`test_completion_target_resolution.py` — 11 tests. The incident shape is
`test_unresolved_reminder_beats_older_active_task` and
`test_node_does_not_patch_the_stale_page`; the latter is the load-bearing one,
asserting no Notion write lands on the stale page.

Also covered: recency precedence in both directions, the TTL boundary, missing
`selected_at` failing closed, reminder targets not rewriting Notion, the matched
row being cleared, the ask-don't-guess path granting no streak credit, and a
reminder-lookup failure still completing a genuinely active task.

`tests/integration/test_recent_outbound_reader.py` exercises the SQL predicates
against a real database (skipped without `DATABASE_URL`): expiry, already-resolved
rows, per-peer scoping, recency ordering, and idempotent clearing.

## Lesson

A spec that describes a consumer does not mean the consumer exists. When a
table has a producer, a schema, a retention policy, and three documents
describing how it is read, that reads as a finished feature — `grep` for the
reader before assuming it is one.

And when a resolution step has no confident answer, fail closed. The pre-fix
node had two possible referents and no way to compare them, so it used the one
it happened to hold. Asking the user one question is always recoverable;
closing the wrong task is not.
