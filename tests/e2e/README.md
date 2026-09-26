# E2E conversation layer

Scripted multi-turn conversations driven through the **real** compiled LangGraph
graph, against a **real** Postgres checkpointer, using the **real** LLM via the
LiteLLM proxy. Notion and Signal are faked (`tests/support/`); the model is not.

The unit of test is a conversation, not a node call. Every other layer calls node
functions directly with a hand-built `State` dict, which cannot observe the seam
where turn N writes state and turn N+M reads it — the seam bug #641 lived on.

## Running locally

```bash
docker run -d --rm --name hml-e2e-pg \
  -e POSTGRES_USER=hml -e POSTGRES_PASSWORD=hml -e POSTGRES_DB=hml \
  -p 5432:5432 postgres:16-alpine

bash scripts/ci-local.sh e2e
```

`scripts/ci-local.sh e2e [files…]` runs pytest under `env -i`, so pytest
receives only:

- `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`, `VIRTUAL_ENV`, `PYTHONPATH`,
  `TERM` — passed through from the shell when set;
- `ENABLE_E2E_CONVERSATIONS=true`, always;
- `DATABASE_URL`, `LLM_PROXY_BASE_URL`, `LLM_PROXY_API_KEY`,
  `E2E_MAX_LLM_CALLS`, `E2E_DEBUG_TURNS`, `AUTHORIZED_PEERS`,
  `SIGNAL_ACCOUNT`, `REWARD_ARTIFACTS_DIR` — from the shell when set, else
  `.github/workflows/e2e.yml`'s value.

Every other variable never reaches pytest: `OPENAI_API_KEY` (so rewards stay
emoji-only locally too), `E2E_TURN_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`,
`USER_TZ`, tracing controls, and the rest. To change one of those for a run,
use pytest directly (below). The script prints `DATABASE_URL` only as
`host:port/dbname`, never with credentials.

The LLM proxy has exactly one inference slot, shared by `e2e.yml`,
`nightly-evals.yml`, and `model-swap.yml` (the `homelab-llm-serial`
concurrency group) — a local run competing with a CI run corrupts both runs'
latency. So `ci-local.sh e2e` checks all three with `gh run list` and refuses
to start while any has a `queued` or `in_progress` run. It also fails closed:
when `gh` is missing, not authenticated, or the lookup fails, it refuses
rather than guessing. `--force` is the only override, and only `e2e` accepts
it. See
`scripts/ci-local.sh --help` for the other modes (`unit`, `db`, `docs`, `all`).

To run pytest directly instead:

```bash
ENABLE_E2E_CONVERSATIONS=true \
DATABASE_URL=postgresql://hml:hml@localhost:5432/hml \
LLM_PROXY_BASE_URL=https://llm.featherback-mermaid.ts.net/v1 \
LLM_PROXY_API_KEY=fake-key \
SIGNAL_ACCOUNT=+15550009999 \
REWARD_ARTIFACTS_DIR=/tmp/hml-reward-artifacts \
pytest tests/e2e/ -q
```

The database must be named `hml`: `migrations/0005_readonly_user.sql` issues a
literal `GRANT CONNECT ON DATABASE hml`. The proxy is tailnet-only, so this needs
a machine on the tailnet — the same reason the CI job runs on the `homelab`
runner rather than a GitHub-hosted one.

Without `ENABLE_E2E_CONVERSATIONS`, or with any required variable missing, the
whole directory skips.

## Diagnosing a CI failure

The self-hosted `homelab` runner's job log is not retrievable via the API
today, so `.github/workflows/e2e.yml` tees the raw `pytest` output to a file
and uploads it as the `e2e-pytest-log` workflow artifact (`if: always()`,
7-day retention) — download it from the failed run rather than trying to
reconstruct output from the job summary.

`E2E_DEBUG_TURNS` makes a failing invariant or `Expect` assertion inside
`Conversation._turn` print that turn's captured structlog events and the
delivered reply's length before raising. It is off when you run pytest
directly (set `E2E_DEBUG_TURNS=1` to enable it), and on in CI and under
`scripts/ci-local.sh e2e`, which both default it to `true`. Only event names,
booleans, counts, and string values under an explicit key allowlist
(`intent`, `tier`, `node`, `page_id`, …; see `_SAFE_STRING_KEYS` in
`tests/support/harness.py`) are printed — a string under any other key is
dropped however short it is, so message text, titles, and peers never
appear, and the printout is safe to paste into a PR comment or issue.
This is what tells you which intent the classifier chose and which node ran
without re-running the scenario with a debugger attached. An entry logged
from inside an `except` block (a node's `*.error` fallback) also carries
`exception_class` — the exception's class name, never its message — so a
fallback reply in CI names what raised.

## Failure taxonomy

Two failure types, and the distinction matters when triaging:

| Type | Meaning | Response |
|---|---|---|
| `IntentMisrouteError` | The classifier chose a different intent than the scenario declared. The state machine is intact; the **model** disagreed. | Read the prompt diff. If the model is right and the scenario is wrong, fix the scenario. Never add a retry — retrying hides exactly the drift this layer detects. |
| `AssertionError` | An invariant (`tests/support/invariants.py`) or a scenario's own `Expect` contract broke. | A code regression. Treat as a real bug. |

`E2E_MISROUTE_BLOCKING` is not implemented yet; misroutes fail the run. If model
drift proves noisy in practice, that knob is the intended escape hatch.

## Writing a scenario

Assert what the system **did**, not what it **said**. Notion status, which page
ids were written, `recent_outbound.awaiting_reply`, the checkpoint's
`active_task`, and the number of messages sent are all deterministic under a
nondeterministic model. Wording is not.

```python
await conversation.say(
    "done",
    expect=Expect(
        intent="COMPLETE",
        notion_status={page_a: "Completed"},
        notion_untouched=[page_b],
        db_awaiting_reply=0,
        sent_count=1,
    ),
)
```

`regex_require` / `regex_forbid` exist but should stay rare; judged text quality
is the eval layer's job, and it has a model to score it with.

Two rules that are easy to get wrong:

- **Deliver reminders through `conversation.deliver_reminder()`**, never a
  fixture `INSERT INTO recent_outbound`. That INSERT in `reminder_worker` is the
  table's only writer and the row a later COMPLETE resolves against; a fixture
  insert would keep passing with the production INSERT deleted, which is
  precisely the pre-#641 state of the world. Pass `kind="deadline"` for a
  deadline nudge; the worker then leaves the task open and records
  `reminder_type='deadline'`.
- **Seed preconditions with `seed_active_task()` / `age_active_task()` and
  `seed_recent_tasks()` / `age_recent_tasks()`**, not by running extra live
  turns. It keeps the assertion pointed at the seam and cuts the LLM calls a
  scenario costs. `outbox_state(page_id)` reads a page's `reminder_outbox`
  states when a scenario needs to prove a reminder will or will not fire.

The invariants in `tests/support/invariants.py` run after every turn
automatically. A scenario only needs to state what is specific to itself.

### Post-send interaction review

Every fixture except `conversation_with_review` runs the listener with the
interaction review off, so a scenario's `sent_count` counts only the turn's
own replies. With `conversation_with_review`, call
`conversation.settle_review(expect=Expect(...))` after a turn: it waits for
that turn's background review, captures any follow-up it sent, and runs the
per-turn invariants and the `expect` against it (leave `intent` unset).
`conversation.review_rows()` reads the peer's `interaction_reviews` rows
(`verdict` is the job state, `reason` the skip or error code); after a settle
no row is left `pending`. See
`tests/e2e/scenarios/test_loop_interaction_review.py`.

### Stacked messages

`SignalListener` coalesces into one graph turn (`\n`-joined) same-peer messages
that are already queued when the fixed debounce delay from the first message
expires.
The default `conversation` fixture sets that debounce to 0 so every other
scenario's `say()` maps one-to-one onto one graph call. To test coalescing
itself, use the `conversation_debounced` fixture (2s debounce) with
`Conversation.say_stacked(["first message", "second message"], gap_seconds=1.0)`,
which sends each message through the same `SignalListener` entry path as
`say()`, waits for exactly one turn to complete, and asserts the graph's call
count grew by exactly 1 rather than by the number of messages sent. See
`tests/e2e/scenarios/test_loop_stacked_messages.py`.
