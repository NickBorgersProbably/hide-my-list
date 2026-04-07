// Deterministic verdict aggregator for the v2 review pipeline.
//
// Pure function: array of reviewer artifacts (parsed JSON conforming to
// schema/reviewer-v1.json) + a fix-result artifact -> { verdict, reasons[] }.
//
// Verdicts are binary: "GO" | "NO-GO". NO-GO is the human-escalation path
// (see plan: pipeline does not auto-close PRs and does not auto-create
// replacement issues on NO-GO).
//
// This module is used by the judge job, which runs with permissions:
// contents: read and no git credentials. The judge therefore CANNOT push.
//
// The judge MUST NOT read PR comments or PR Reviews. Reviewers are
// responsible for ingesting inline comments and folding any blocking
// change requests into their artifact's blocking_issues[] (per
// docs/agentic-pipeline-learnings.md rule 1.5).

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
 * @property {string[]} addressed   ids of blocking_issues the fixer applied
 * @property {Array<{id:string,reason:string}>} skipped
 * @property {string} new_sha       resulting SHA after fix (== input sha if no-op)
 */

/**
 * @typedef {Object} Verdict
 * @property {"GO"|"NO-GO"} verdict
 * @property {string[]} reasons     human-readable explanation lines
 * @property {string[]} unaddressed_blocker_ids
 */

/**
 * Aggregate reviewer artifacts and a fix-result into a single verdict.
 *
 * @param {ReviewerArtifact[]} reviewers
 * @param {FixResult} fixResult
 * @returns {Verdict}
 */
export function aggregate(reviewers, fixResult) {
  const reasons = [];
  const addressed = new Set(fixResult?.addressed ?? []);

  if (!Array.isArray(reviewers) || reviewers.length === 0) {
    return {
      verdict: "NO-GO",
      reasons: ["No reviewer artifacts present."],
      unaddressed_blocker_ids: [],
    };
  }

  // Collect all unaddressed blockers across reviewers.
  const unaddressed = [];
  for (const r of reviewers) {
    for (const b of r.blocking_issues ?? []) {
      if (!addressed.has(b.id)) {
        unaddressed.push({ role: r.role, id: b.id, severity: b.severity, message: b.message });
      }
    }
  }

  // Reviewers requesting changes whose blockers were NOT all addressed.
  const requestingChanges = reviewers.filter(
    (r) =>
      r.decision === "request_changes" &&
      (r.blocking_issues ?? []).some((b) => !addressed.has(b.id))
  );

  if (unaddressed.length > 0) {
    reasons.push(
      `${unaddressed.length} blocking issue(s) not addressed by fixer: ` +
        unaddressed.map((u) => `${u.role}/${u.id}`).join(", ")
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
      unaddressed_blocker_ids: unaddressed.map((u) => `${u.role}/${u.id}`),
    };
  }

  const nonAbstaining = reviewers.filter((r) => r.decision !== "abstain");
  if (nonAbstaining.length === 0) {
    reasons.push("All reviewers abstained; treating as vacuous GO.");
  } else {
    reasons.push(
      `All ${nonAbstaining.length} non-abstaining reviewer(s) cleared; ` +
        `${addressed.size} blocker(s) addressed by fixer.`
    );
  }

  return { verdict: "GO", reasons, unaddressed_blocker_ids: [] };
}
