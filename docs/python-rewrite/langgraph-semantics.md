# LangGraph Semantics — Durability Spike Findings

## Purpose

This document records findings from the Phase B spike validating LangGraph + PostgresSaver
behavior under the conditions Phase B/C depend on. Four areas investigated:

1. Per-peer thread isolation under concurrency
2. Restart mid-turn behavior with PostgresSaver
3. Worker-to-graph state read pattern
4. Schema migration (adding/removing State fields, reading old checkpoints)

Spike code lives in `tests/spike/`. All findings use LangGraph 1.2.0 (pinned in
`pyproject.toml`) and `langgraph-checkpoint-postgres` 3.1.0.

---

## Finding 1 — Per-Peer Thread Isolation

**Question:** Do two simultaneous `graph.ainvoke()` calls with different `thread_id`s bleed
state into each other?

**Answer:** No — LangGraph uses `thread_id` as the checkpoint partition key. Each
`(thread_id, checkpoint_ns)` tuple has its own independent checkpoint row. Concurrent
invocations with different `thread_id`s do not share checkpoints.

**Implementation:** In `app/ingress/signal_listener.py` we pass
`config={"configurable": {"thread_id": peer}}` where `peer` is the E.164 Signal sender.
This is the partition key. Two peers cannot share state.

**Tested in:** `tests/spike/test_thread_isolation.py` — starts two concurrent
`ainvoke` calls via `asyncio.gather`, verifies each peer's `pending_outbound` is
independent.

**Caveat:** Thread isolation is enforced by the caller passing distinct `thread_id` values.
If the signal listener ever passes the same `thread_id` for two different peers (e.g., due
to a bug), state would merge. Peer field validation at node entry is a future hardening item
for Phase C — not yet implemented in Phase B intake.

---

## Finding 2 — Restart Mid-Turn

**Question:** If the process is killed during a node execution, does the next invocation
resume from the last successful super-step checkpoint or from a partial node result?

**Answer:** LangGraph checkpoints at super-step boundaries, not inside node execution.
A node that writes partial state but crashes mid-execution will have its changes discarded.
On the next invocation, the graph replays from the last successfully committed super-step.

**How this works:** `AsyncPostgresSaver` writes the checkpoint (state snapshot) in a single
Postgres transaction after each super-step completes. A node crash before the transaction
commits means no checkpoint is written. The next `ainvoke` sees the pre-crash state
and re-executes the crashed node from scratch.

**Implications for hide-my-list:**
- Nodes must be idempotent with respect to external side effects (Notion writes, Signal sends).
- The `send` terminal node uses idempotency keys. If it sends a Signal message and then
  crashes before the checkpoint commits, the next run will retry the send. The idempotency
  key is generated deterministically from `(peer, incoming_hash)` so signal-cli can
  deduplicate where supported.
- The intake node creates Notion tasks. If it creates a task and crashes, the next run
  creates the task again. Mitigation (future work): check for existing task with the same
  `idempotency_key` field before creating. The Phase B intake node does not yet implement
  this check — it is tracked as a hardening item for Phase C.

**Tested in:** `tests/spike/test_restart_semantics.py` — injects a mock node that raises
on first call, verifies the next invocation re-enters the node cleanly.

---

## Finding 3 — Worker-to-Graph State Read Pattern

**Question:** A worker (outside the graph) writes a `recent_outbound` row. How does the
next graph turn read it?

**Answer:** `recent_outbound` is NOT part of LangGraph State/checkpoint. It lives in a
dedicated Postgres table. Graph nodes read it at turn start via a direct DB query.

**Why this design:** LangGraph checkpoints are immutable after each super-step. A worker
running outside the graph cannot mutate a checkpoint without invoking the graph. Storing
`recent_outbound` in the checkpoint would require the worker to know the thread_id partition
and directly write to checkpoint tables — coupling the worker to LangGraph internals.
Storing it in a plain Postgres table decouples the worker completely.

**Pattern (target — not yet implemented in Phase B classifier):**

```python
# Target pattern for classify_intent (Phase B classifier does not yet read this):
async def classify_intent(state: State) -> dict:
    peer = state["peer"]
    # Read recent_outbound from Postgres — NOT from checkpoint state
    async with get_db_conn() as conn:
        rows = await conn.fetch(
            "SELECT * FROM recent_outbound WHERE peer = $1 AND awaiting_response = true "
            "AND expires_at > now() ORDER BY sent_at DESC LIMIT 5",
            peer
        )
    recent_outbound_context = build_context_string(rows)
    # Use context to resolve ambiguous intents
    intent = await llm_classify(state["incoming"], recent_outbound_context)
    return {"intent": intent}
```

The Phase B implementation in `app/graph/routing.py` passes `RECENT_OUTBOUND_CONTEXT`
as a placeholder in the classifier prompt but does not yet query the `recent_outbound`
table. The Phase B prompt template references the variable, and the node must supply it.
Full DB-backed recent_outbound read is targeted for Phase C once the worker populates
the table during live operation.

**Worker writes:**

```python
# In app/scheduler/reminder_worker.py, after successful Signal send:
await conn.execute(
    """
    INSERT INTO recent_outbound
      (peer, signal_timestamp, notion_page_id, title, reminder_type, sent_at, expires_at)
    VALUES ($1, $2, $3, $4, 'reminder', now(), now() + interval '24 hours')
    ON CONFLICT (peer, signal_timestamp) DO NOTHING
    """,
    peer, signal_ts, notion_page_id, task_title
)
```

**Tested in:** `tests/spike/test_worker_graph_read.py` — verifies that a row written
directly to `recent_outbound` (simulating the worker) is visible to the graph node on the
next `ainvoke`.

---

## Finding 4 — Schema Migration

**Question:** What happens when State fields are added or removed, and old checkpoints are
read?

**Answer:** LangGraph stores checkpoints as serialized JSON. The `AsyncPostgresSaver` uses
`JsonPlusSerializer` by default. Key behaviors:

**Adding a new State field:**
- Old checkpoints don't have the new field.
- When LangGraph loads the checkpoint, the new field is absent in the restored State dict.
- Nodes must handle `state.get("new_field")` potentially returning `None`.
- Mitigation: use `TypedDict` with `total=False` for optional fields, or provide explicit
  `None` defaults in `State.__required_keys__` handling.
- LangGraph does NOT automatically backfill old checkpoints — there is no "migration" of
  checkpoint content.

**Removing a State field:**
- Old checkpoints contain the removed field in JSON.
- FINDING (spike-confirmed): LangGraph strips unknown keys when building State for a node.
  The extra key from old checkpoint JSON is silently dropped — nodes see `None` (via
  `.get()`) rather than the old value. This is safe: removed fields are invisible, not errors.
- Implication: if a field is removed from State TypedDict, nodes reading `.get("removed_field")`
  will get `None` even if old checkpoint JSON contains the key. The old data is effectively
  inaccessible without a migration. For this app, this is acceptable — no removed fields
  carry critical live data.

**Practical rule for hide-my-list:**
- Additive changes (new optional fields): safe, nodes use `.get()`.
- Removals: safe at runtime, but leave documentation to avoid confusion.
- Renames: treat as remove + add. Old checkpoint data is orphaned under the old key.

**Schema migration contract:**
- LangGraph checkpoint tables are managed by `AsyncPostgresSaver.setup()` (called at startup).
- Application-level schema changes live in `migrations/` and are applied before app start.
- Checkpoint schema (`checkpoints`, `checkpoint_writes`, `checkpoint_migrations` tables) is
  LangGraph-owned — do not manually edit.

**Tested in:** `tests/spike/test_schema_migration.py` — writes an old-format checkpoint
manually, then reads it with a "new" State that has an extra field, verifies the new field
defaults correctly.

---

## Summary

| Concern | Status | Notes |
|---------|--------|-------|
| Per-peer isolation | Confirmed safe | `thread_id=peer` is the partition key |
| Restart mid-turn | Confirmed predictable | Super-step boundary checkpointing; idempotency required |
| Worker→graph read | Pattern validated | `recent_outbound` in plain Postgres table, read at turn start |
| Schema migration | Understood, manageable | Additive OK; removals safe; renames require care |

No deal-breakers found. Phase B/C can proceed on the current LangGraph + PostgresSaver stack.

---

## LangGraph Version Notes

All findings apply to `langgraph==1.2.0` with `langgraph-checkpoint-postgres==3.1.0`.
Upgrading either library may change checkpoint serialization format or super-step semantics.
Pin both in `pyproject.toml` and run spike tests after any version bump.
