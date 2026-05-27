# Test Rig: hide-my-list Behavioral + LLM-Swap Test Architecture

Authoritative reference for the test rig that enforces behavioral contracts,
catches dead-code wiring, and validates model-swap readiness across the
Python + LangGraph stack.

The rig review enforcer lives at `.github/scripts/review/prompts/test.md` and
fires on every PR that touches `app/**`, `migrations/**`, `setup/model-tiers.json`,
`app/prompts/**`, or `docs/ai-prompts/**`.

---

## Layer Architecture

| Layer | Directory | LLM | Postgres | Compose | Responsibility |
|---|---|---|---|---|---|
| Unit | `tests/unit/` | mocked (`MagicMock`) | none | no | Pure logic, prompt structure, regex/type assertions, structural lints |
| Integration | `tests/integration/` | mocked (with strict call-arg assertions) | real (container) | no | State machines, DB schema, async plumbing, wiring contracts |
| Eval | `tests/evals/` | real, multi-model via LiteLLM proxy | real | no | Behavioral contracts across model swaps; judge-LLM scoring |
| Smoke | `tests/smoke/` | one real call via LiteLLM proxy | real | yes (boots stack) | Deployment-gap catch |
| Regressions | `tests/regressions/` | varies | varies | varies | One permanent test per production bug |

### Cost and frequency

| Layer | Wall time | LLM cost | Frequency |
|---|---|---|---|
| Unit | <30 s | $0 | every commit |
| Integration | <2 min | $0 | every commit |
| Structural lints | <10 s | $0 | every commit |
| Compose smoke | <3 min | <$0.01 | on main / nightly |
| Evals (baseline) | 10-20 min | ~$2-5 | nightly |
| Model-swap report | 15-30 min | ~$5-10 | manual `workflow_dispatch` |

CI never sets `ENABLE_LIVE_LLM_EVALS=true` by default. Nightly workflow sets
baseline tiers only. Model-swap workflow is `workflow_dispatch` with a
`candidate_model` input.

---

## The Eight Bug Classes

Each bug class leaves a permanent test. Fix -> regression test ->
`tests/regressions/bug_<NNNN>_<slug>/`. The catalog grows; we don't relearn.

| # | Bug | Where tested | Key assertion |
|---|---|---|---|
| 1 | psycopg3 UUID coercion | `tests/regressions/bug_0570_reminder_uuid_coercion/test_uuid_round_trip.py` | Insert via `reminders.enqueue`, dispatch, assert no `AttributeError`, `state='delivered'`, correct recipient kwarg |
| 2 | LLM capability denial | `tests/unit/test_no_capability_denial.py` (structural) + `tests/evals/test_chat_capability_contract.py` (eval, PR-2) | `regex_forbid` denial phrasing; judge score >= 0.7 for capability acknowledgment |
| 3 | Image orphaned from delivery | `tests/integration/test_reward_image_delivery.py` (follow-up) | `mock.call_args.kwargs["attachment_path"]` is non-None AND file exists; not just `mock.called` |
| 4 | Auth gate | `tests/unit/test_signal_listener_auth.py` (existing) + `tests/smoke/test_compose_auth_closed.py` (PR-2) | Unauthorized peer rejected; no state-table writes |
| 5 | Deployment gaps | `tests/smoke/test_compose_round_trip.py` (PR-2) | Full compose stack boots; reminder_outbox table exists; env vars threaded |
| 6 | Dead-code wiring | `tests/unit/test_reachability.py` (AST scan) | Every public top-level function in scanned dirs has >= 1 call site outside its definition |
| 7 | Migration filename collisions | `tests/unit/test_migration_filenames.py` | Unique prefixes, monotonic sequence, format `\d{4}_[a-z][a-z0-9_]*.sql` |
| 8 | mypy suppression sprawl | `tests/unit/test_mypy_suppression_budget.py` | Count of `ignore_errors = true` overrides matches frozen baseline; can only shrink |

---

## Structural Lints (unit speed, always runs)

Four lints in `tests/unit/` that catch four of the eight bug classes without
LLM or Postgres:

### `test_migration_filenames.py`

Globs `migrations/*.sql`, parses the `^\d+_` prefix, asserts:
- All prefixes are unique (no duplicate `0005_*.sql`).
- Prefixes are monotonic starting at 1 with no gaps.
- Filenames match `^\d{4}_[a-z][a-z0-9_]*\.sql$`.

Currently `test_prefixes_are_unique` is `xfail` because main has two
`0005_*.sql` files (known collision; cleanup is a follow-up).

### `test_reachability.py`

AST-scans `app/tools/*.py`, `app/graph/nodes/*.py`, `app/scheduler/*.py`,
`app/ingress/*.py` for top-level public functions (no leading `_`). For each
function, counts total occurrences (word-boundary regex) across all `app/**/*.py`
files. A function with exactly 1 occurrence (the definition line) has no callers
— that is dead-code wiring.

Two exemption sets:
- `_ENTRY_POINTS`: APScheduler callbacks and `main.py` entry points where the
  only reference is the framework registration in the same file.
- `_KNOWN_DEAD`: pre-existing dead code that predates this test. Removing a
  name from here (after fixing the source) is encouraged. Adding a new name
  requires PR justification.

### `test_mypy_suppression_budget.py`

Parses `[[tool.mypy.overrides]]` blocks with `ignore_errors = true` from
`pyproject.toml`. Asserts count matches `BASELINE_COUNT` and module names match
`BASELINE_MODULES`. Currently baseline is 0 (no suppressions). Adding a
suppression requires updating both constants with a PR comment explaining the
rationale and a plan for removal.

### `test_model_tier_swap_surface.py`

Two assertions:
1. `setup/model-tiers.json` parses as JSON with exactly `{expensive, medium, cheap, reminder}` keys and non-empty string values.
2. No Python file in `app/` (except `app/models.py`) contains a hardcoded model
   identifier matching `claude-(opus|sonnet|haiku)|gpt-|gemma`.

Pre-existing violations (before this test was introduced) are listed in
`_KNOWN_VIOLATIONS` as `(relative_path, line_number)` pairs. New violations
always fail. Cleaning up a known violation means removing it from both the
source and the set.

---

## Regression Directory Convention

```
tests/regressions/
  bug_<NNNN>_<slug>/
    README.md        # Bug story: what broke, why, issue/PR link
    __init__.py
    test_*.py        # At least one test, OR README notes "test lives in ..."
```

`NNNN` is the canonical GitHub issue or PR number (whichever tracks the bug).
The `README.md` must contain a `#NNN` reference. The structural lint
`tests/unit/test_regression_catalog.py` enforces this shape on every PR.

Standalone run: `pytest tests/regressions/bug_0570_reminder_uuid_coercion -v`

Seeded entries (from PR-1):
- `bug_0570_reminder_uuid_coercion/` — full integration test (skips without `DATABASE_URL`)
- `bug_0567_capability_denial/` — README + pointer; test in eval layer (PR-2)
- `bug_0563_reward_image_orphan/` — README + pointer; test in integration layer (follow-up)

---

## Eval Layer: Fixture Format

Each fixture in `tests/evals/fixtures/<node>/<scenario>.yaml` describes one
model-under-test scenario:

```yaml
id: chat-missed-reminder-001
node: chat
peer: "<test-peer-1>"
inbound: "you didn't remind me - why not?"
prior_state:
  last_reminder_at: "2026-05-26T14:00:00Z"
  active_task_title: "<placeholder>"
tier: medium
contracts:
  - kind: regex_forbid
    pattern: "(?i)(can(not|'t)|not able to)\\s+send\\s+reminders"
  - kind: regex_require
    pattern: "(?i)(missed|did(n'?t| not) (arrive|fire))"
  - kind: judge
    rubric: "Response acknowledges the system DOES send reminders and offers a concrete forward step."
    threshold: 0.7
  - kind: shame_safe
    threshold: 0.8
```

Contract kinds:
- `regex_forbid` / `regex_require` — deterministic; no LLM
- `json_schema` — pydantic validation of structured outputs (intake node)
- `judge` — qualitative rubric scored by a stronger judge LLM (Sonnet 4.6)
- `shame_safe` — judge with fixed ADHD-safety rubric from `design/adhd-priorities.md`

Privacy invariant: all fixtures use placeholder values (`<test-peer>`,
`<placeholder>`, random UUIDs). No real user data in fixtures, commits, or
judge LLM payloads.

Eval layer is PR-2. Per-node fixture coverage target: >= 5 fixtures per node
across 9 nodes (intake, selection, chat, rejection, cannot_finish, need_help,
check_in, complete, classify_intent).

---

## Integration Mock Discipline

Integration tests that mock outbound side effects (Signal send, Notion write,
image gen) must assert the SHAPE of the call, not just the fact of the call.

**Wrong (bug class 3 pattern):**
```python
signal_mock.assert_called()
```

**Right:**
```python
signal_mock.assert_awaited_once()
assert signal_mock.await_args.kwargs["recipient"] == "<test-peer>"
assert signal_mock.await_args.kwargs["attachment_path"] is not None
```

This distinction matters because `assert_called()` passed even when
`signal_client.send_message` was called without an `attachment_path`,
meaning every reward image was silently discarded.

---

## LLM Swap: How It Works

The eval runner rewrites `setup/model-tiers.json` for the test session and
points `MODEL_TIERS_PATH` env at the modified copy. `app.models.llm(tier)` is
unchanged — it reads the env-pointed JSON. `ChatAnthropic` talks to LiteLLM
at `ANTHROPIC_BASE_URL`; LiteLLM dispatches by model alias. Swapping
`cheap: claude-haiku-4-5` to `cheap: gemma4-small` is a one-line change in
`setup/model-tiers.json`.

No Python adapter branching. All LLM routing stays through `app/models.py:llm(tier)`.

Three cost gates for eval runs:
- `ENABLE_LIVE_LLM_EVALS=true` — required for any real LLM call; absent = `pytest.skip`
- `EVAL_MODELS` — explicit comma-separated model alias allowlist; empty = no evals
- `EVAL_BUDGET_USD` — soft cap; runner halts with `pytest.fail("budget exceeded")`

---

## Test Discipline Rules (Developer-Facing)

These are the six contract clauses the test reviewer enforces (see
`.github/scripts/review/prompts/test.md` for the authoritative enforcement spec):

1. **New public function in `app/tools/`, `app/graph/nodes/`, `app/scheduler/`, `app/ingress/`** must have:
   - An integration test asserting reachability from an end-to-end flow.
   - For DB-typed fields (UUID, timestamp, JSON): an integration test with real Postgres round-trip.
   - For outbound side effects: assertion on `mock.call_args.kwargs` shape, not just `mock.called`.

2. **New or modified prompt template in `app/prompts/`** must have:
   - Updated structural test if it adds a new section anchor.
   - New eval fixture if behavior contract changed (new capability statement, new banned phrase).

3. **New migration in `migrations/`** must:
   - Use the next monotonic integer prefix (structural lint enforces this).
   - For schema changes touching code-read columns: include an integration test.

4. **New env var or compose service** must have:
   - Assertion in `tests/smoke/test_compose_round_trip.py` that it's threaded through.
   - Documentation in `docker/compose.yaml` comments.

5. **PR fixing a production bug** must add:
   - `tests/regressions/bug_<NNNN>_<slug>/` directory with README citing issue/PR.
   - At least one `test_*.py`, or README note "test lives in ...".

6. **Dropped tests** need explicit PR-body justification. Silent deletion of a failing test is always a blocker.

If this PR adds a new bug class or extends the layer architecture described in
this document, update this document AND update
`.github/scripts/review/prompts/test.md` to enforce the new contract.
