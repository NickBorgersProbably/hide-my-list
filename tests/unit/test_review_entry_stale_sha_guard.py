"""Structural lint: review-entry.yml stale-SHA guard.

Asserts that review-entry.yml refuses to dispatch a review cycle for a SHA
that is no longer the PR head, and that every downstream gate honours the
guard's output.

Bug class prevention: `pull_request` runs serialize in the
`review-entry-v2-<head_ref>` concurrency group (`cancel-in-progress: false`),
so a run can un-queue minutes after the SHA that triggered it was superseded.
Without the guard, a burst of rapid pushes spends one full reviewer fan-out
plus fixer and judge per push, and a NO-GO on a superseded SHA applies
`needs-human-review` describing code that no longer exists at HEAD. Observed
on PR #621: three commits 35s apart, two full cycles, the first finalizing
NO-GO against a SHA two commits stale.
"""
from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
_ENTRY_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "review-entry.yml"

_GUARD_STEP = "Check reviewed SHA is still PR head"
_GUARD_GATE = "steps.freshness.outputs.stale != 'true'"


def _workflow_text() -> str:
    return _ENTRY_WORKFLOW.read_text(encoding="utf-8")


def test_guard_step_present() -> None:
    """review-entry.yml must contain the stale-SHA guard step."""
    text = _workflow_text()
    assert _GUARD_STEP in text, (
        f"Expected a '{_GUARD_STEP}' step in review-entry.yml. Without it, a "
        "review cycle that un-queues after newer pushes have landed reviews a "
        "superseded SHA. See PR #621."
    )


def test_guard_compares_against_live_pr_head() -> None:
    """The guard must read the live head SHA from the PR, not the event payload.

    The event payload SHA is what triggered the run and is stale by
    construction in exactly the case the guard exists to catch.
    """
    text = _workflow_text()
    assert 'gh api "repos/${REPO}/pulls/${PR_NUMBER}" --jq \'.head.sha\'' in text, (
        "The stale-SHA guard must resolve the live head via the pulls API. "
        "Comparing the event payload SHA against itself is a no-op."
    )


def test_guard_runs_before_dedup_claim() -> None:
    """The guard must precede the dedup claim.

    A claim written for a stale SHA leaves a dangling `review/pipeline`
    pending status that nothing subsequently cleans up.
    """
    text = _workflow_text()
    guard_pos = text.find(_GUARD_STEP)
    dedup_pos = text.find("SHA-keyed dedup claim")
    assert guard_pos != -1, f"'{_GUARD_STEP}' step not found in review-entry.yml"
    assert dedup_pos != -1, "'SHA-keyed dedup claim' step not found"
    assert guard_pos < dedup_pos, (
        "The stale-SHA guard must appear before the dedup claim so a stale "
        "cycle never claims a SHA it will not review."
    )


def test_dedup_claim_is_gated_on_guard() -> None:
    """The dedup claim step must skip when the guard reports a stale SHA."""
    text = _workflow_text()
    dedup_pos = text.find("SHA-keyed dedup claim")
    gate_pos = text.find(_GUARD_GATE, dedup_pos)
    assert gate_pos != -1, (
        f"Expected '{_GUARD_GATE}' in the dedup claim step's if: condition. "
        "An ungated claim dispatches the pipeline for a superseded SHA."
    )


def test_pipeline_job_is_gated_on_guard() -> None:
    """The pipeline job must carry a redundant stale-SHA skip.

    Mirrors the belt-and-braces pattern the cycle cap uses (§1.3): the step
    gate and the job gate fail independently.
    """
    text = _workflow_text()
    assert "needs.resolve.outputs.stale_sha != 'true'" in text, (
        "Expected the pipeline job's if: condition to include "
        "\"needs.resolve.outputs.stale_sha != 'true'\". Step-level gating "
        "alone leaves the dispatch reachable if the dedup gate is edited."
    )


def test_guard_is_scoped_to_pull_request_events() -> None:
    """The guard must not fire on `issue_comment` (`/review`) runs.

    `/review` resolves `reviewed_sha` from the API at run time, so it is head
    by construction. Applying the guard there would make the human override
    depend on a race it cannot lose but should not have to run.
    """
    text = _workflow_text()
    guard_pos = text.find(_GUARD_STEP)
    # The step's `if:` block ends where its `env:` block begins.
    env_pos = text.find("env:", guard_pos)
    guard_block = text[guard_pos:env_pos]
    assert "github.event_name == 'pull_request'" in guard_block, (
        "The stale-SHA guard must be event-gated to `pull_request` runs."
    )
