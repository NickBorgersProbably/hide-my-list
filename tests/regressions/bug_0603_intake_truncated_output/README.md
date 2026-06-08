# Bug 0603: Truncated Intake Output

**Issue:** #603

## Bug Story

Medium-tier intake responses could be truncated before the JSON object was
complete. The parser converted that malformed output into a fake successful
plain-task save, so reminder-shaped requests were acknowledged without creating
reminder scheduling state.

## Regression Tests

Tests live in `tests/unit/test_intake_parse.py`,
`tests/integration/test_intake.py`, and
`tests/evals/fixtures/intake/remind_deadline_before_time.yaml`.
