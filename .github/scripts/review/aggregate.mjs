// Internal judge-stage helper for the review pipeline.
//
// SCOPE: this module defines the read-only judge-stage verdict
// (binary GO | NO-GO) emitted by the judge job, consuming structured
// reviewer artifacts conforming to `schema/reviewer-v1.json`. See
// docs/agentic-pipeline-learnings.md §1.4 + §1.5 for the contract
// between reviewers and judge, and DEV-AGENTS.md "Review Pipeline"
// for the workflow graph that invokes this module.
//
// Pure function: array of reviewer artifacts (parsed JSON conforming
// to schema/reviewer-v1.json) + a fix-result artifact -> verdict.
// No I/O, no Codex, no git. Designed to run in a job with
// permissions: contents: read so the judge structurally cannot push.
//
// FAIL-CLOSED CONTRACT
//
// The aggregator rejects mixed review epochs and ambiguous inputs by
// returning NO-GO with a `reasons[]` line explaining why. The review
// pipeline should hand the judge a coherent set, but this module does
// not assume that — it validates:
//
//   1. All reviewer artifacts share the same `reviewed_sha`.
//   2. All reviewer artifacts share the same `cycle`.
//   3. The fix-result's `input_sha` (the SHA the fixer started from)
//      matches the reviewers' `reviewed_sha`.
//   4. Blocker resolution is keyed by namespaced `role/id`, not by
//      bare `id`, so two reviewers emitting the same id cannot
//      cross-clear each other's blockers. The fixer must therefore
//      emit `addressed[]` entries as `"<role>/<id>"` strings.
//   5. The empty-reviewer set is NO-GO ("no reviewers ran").
//   6. The all-abstain case is NO-GO ("no applicable reviewers"),
//      not a vacuous GO. The pipeline should ensure at least one role
//      always applies, but the aggregator fails closed if it doesn't.
//
// Reviewers are responsible for ingesting inline PR comments via
// `gh api` and folding any blocking change requests into their own
// `blocking_issues[]` (with `source: "inline_comment"`), so the
// judge never has to read PR comments or PR Reviews. This honors
// agentic-pipeline-learnings.md §1.5 from the judge's side; the
// reviewer-side authority chain is documented in the active pipeline
// docs and reviewer prompts.

/**
 * @typedef {Object} BlockingIssue
 * @property {string} id
 * @property {"critical"|"high"|"medium"} severity
 * @property {string} message
 */

/**
 * @typedef {Object} ReviewerArtifact
 * @property {"1"} schema_version
 * @property {string} role
 * @property {string} reviewed_sha
 * @property {number} cycle
 * @property {"approve"|"request_changes"|"comment"|"abstain"} decision
 * @property {string} summary
 * @property {BlockingIssue[]} blocking_issues
 * @property {Array<{id:string}>} fix_suggestions
 */

/**
 * @typedef {Object} FixResult
 * @property {string} input_sha    SHA the fixer started from; MUST equal reviewers' reviewed_sha
 * @property {string} new_sha      resulting SHA after fix (== input_sha if no-op)
 * @property {string[]} addressed  namespaced ids of blockers the fixer applied, e.g. "security/sec-1"
 * @property {Array<{id:string,reason:string}>} skipped
 */

/**
 * @typedef {Object} Verdict
 * @property {"GO"|"NO-GO"} verdict
 * @property {string[]} reasons     human-readable explanation lines
 * @property {string[]} unaddressed_blocker_ids
 */

/** Build a namespaced blocker id: "<role>/<id>". */
function nsId(role, id) {
  return `${role}/${id}`;
}

/**
 * Aggregate reviewer artifacts and a fix-result into a single verdict.
 * Fails closed on any input inconsistency.
 *
 * @param {ReviewerArtifact[]} reviewers
 * @param {FixResult} fixResult
 * @returns {Verdict}
 */
export function aggregate(reviewers, fixResult) {
  const reasons = [];

  if (!Array.isArray(reviewers) || reviewers.length === 0) {
    return {
      verdict: "NO-GO",
      reasons: ["No reviewer artifacts present."],
      unaddressed_blocker_ids: [],
    };
  }

  // (1) All reviewers must share reviewed_sha.
  const shas = new Set(reviewers.map((r) => r.reviewed_sha));
  if (shas.size > 1) {
    return {
      verdict: "NO-GO",
      reasons: [
        `Reviewer artifacts span multiple reviewed_sha values (${[...shas].join(", ")}); refusing to aggregate mixed epochs.`,
      ],
      unaddressed_blocker_ids: [],
    };
  }
  const reviewedSha = [...shas][0];

  // (2) All reviewers must share cycle.
  const cycles = new Set(reviewers.map((r) => r.cycle));
  if (cycles.size > 1) {
    return {
      verdict: "NO-GO",
      reasons: [
        `Reviewer artifacts span multiple cycle values (${[...cycles].join(", ")}); refusing to aggregate mixed epochs.`,
      ],
      unaddressed_blocker_ids: [],
    };
  }

  // (3) Fix-result must reference the same input SHA as the reviewers.
  if (!fixResult || typeof fixResult !== "object") {
    return {
      verdict: "NO-GO",
      reasons: ["Missing fix-result artifact."],
      unaddressed_blocker_ids: [],
    };
  }
  if (fixResult.input_sha !== reviewedSha) {
    return {
      verdict: "NO-GO",
      reasons: [
        `Fix-result input_sha (${fixResult.input_sha}) does not match reviewers' reviewed_sha (${reviewedSha}); refusing to aggregate mixed epochs.`,
      ],
      unaddressed_blocker_ids: [],
    };
  }

  // (4) Resolve blockers by NAMESPACED id ("<role>/<id>").
  const addressed = new Set(Array.isArray(fixResult.addressed) ? fixResult.addressed : []);

  const unaddressed = [];
  for (const r of reviewers) {
    for (const b of r.blocking_issues ?? []) {
      const key = nsId(r.role, b.id);
      if (!addressed.has(key)) {
        unaddressed.push({ role: r.role, id: b.id, key, severity: b.severity, message: b.message });
      }
    }
  }

  // Reviewers whose decision is request_changes AND who still have unaddressed blockers.
  const requestingChanges = reviewers.filter(
    (r) =>
      r.decision === "request_changes" &&
      (r.blocking_issues ?? []).some((b) => !addressed.has(nsId(r.role, b.id)))
  );

  if (unaddressed.length > 0) {
    reasons.push(
      `${unaddressed.length} blocking issue(s) not addressed by fixer: ` +
        unaddressed.map((u) => u.key).join(", ")
    );
  }
  if (requestingChanges.length > 0) {
    reasons.push(
      `Reviewers requesting changes with unaddressed blockers: ` +
        requestingChanges.map((r) => r.role).join(", ")
    );
  }
  if (unaddressed.length > 0 || requestingChanges.length > 0) {
    return {
      verdict: "NO-GO",
      reasons,
      unaddressed_blocker_ids: unaddressed.map((u) => u.key),
    };
  }

  // (6) All-abstain is NO-GO, not a vacuous GO.
  const nonAbstaining = reviewers.filter((r) => r.decision !== "abstain");
  if (nonAbstaining.length === 0) {
    return {
      verdict: "NO-GO",
      reasons: ["All reviewers abstained; no applicable reviewer cleared this change."],
      unaddressed_blocker_ids: [],
    };
  }

  reasons.push(
    `All ${nonAbstaining.length} non-abstaining reviewer(s) cleared; ` +
      `${addressed.size} blocker(s) addressed by fixer.`
  );
  return { verdict: "GO", reasons, unaddressed_blocker_ids: [] };
}
