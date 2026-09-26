# Bug 0683: Reasoning Turns Run Into the Proxy Timeout and Cascade

**PR:** #683

## Bug Story

Every call on the medium, expensive, and reminder tiers left model reasoning
("think") on. On the single-slot model host, intake spent 1.9k–4.3k
completion tokens per turn (47–103 s) and selection 2.1k–2.5k (58–110 s). The
LiteLLM proxy gives up on any single upstream request after 110 s, so a chain
of thought that ran long returned 500: the node took its exception fallback
("Something hiccupped on my end…"), the user's task was not stored, and the
model host kept generating the abandoned request — so the retry and every
queued call waited behind it and timed out too. A classify call that takes
0.5 s was observed waiting 44 s. The same cascade failed the e2e suite in CI
four runs out of five and put ERR rows in the nightly evals.

## Fix

Reasoning is decided per caller. Every call sends an explicit `think` flag;
it is true by default only for `cannot_finish`, `need_help`, and the
background `interaction_review`, the callers whose eval accuracy dropped
without reasoning. Intake, selection, and every other call run think=off
(intake: 49 s → 12 s median, 11/11 fixtures; selection: 50 s → 5 s, 5/5 once
the node sends the user's message as the human turn and the prompt says a
"not stated" time or mood never means nothing fits).
`LLM_REASONING_CALLERS` replaces the default set per deployment; an empty
value turns reasoning off everywhere.

## Regression Test

`test_reasoning_off_by_default.py` builds the model for every tier and caller
through the real factory and asserts the request body carries
`think: false` for intake and the reply nodes, `think: true` only for the
default reasoning callers, and that `LLM_REASONING_CALLERS` replaces the set.
