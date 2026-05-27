# Bug 0567: LLM Capability Denial

**Issue:** (no separate issue filed; PR #567 is the canonical reference)
**Fix PR:** #567

## Bug Story

When a user asked "you didn't remind me - why not?", the CHAT-routed LLM
responded "I'm not able to send reminders." The LLM hallucinated a capability
limitation that does not exist: the system DOES send reminders via signal-cli.

Structural prompt tests (which check prompt file contents without invoking the
LLM) could not catch this class of bug because it depends on the model's
interpretation of the prompt, not the prompt's structure. The fix added explicit
language to the chat prompt template forbidding denial of reminder capability.

## Regression Tests

**Structural (unit layer, CI gate):** test lives in
`tests/unit/test_no_capability_denial.py`. Checks prompt template contents for
absence of denial-enabling phrases. Runs on every PR without LLM calls.

**Behavioral (eval layer, PR-2):** `tests/evals/test_chat_capability_contract.py`
runs the chat node against a real LLM with the fixture
`tests/evals/fixtures/chat/missed_reminder.yaml`. That fixture includes a
`regex_forbid` contract against denial phrasing and a `judge` contract requiring
capability acknowledgment >= 0.7. This test lives in PR-2 (eval layer).
