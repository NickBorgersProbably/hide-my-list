// Deterministic verdict aggregator for the v2 review pipeline's judge stage.
//
// Pure function: reviewer artifacts (parsed JSON conforming to
// schema/reviewer-v1.json) + a fix-result artifact + the expected review epoch
// -> { verdict, reasons[] }.
//
// Verdicts are binary judge-stage outputs: "GO" | "NO-GO". The downstream
// merge-decision stage maps this internal verdict to the final three-state PR
// outcome (GO-CLEAN | GO-WITH-RESERVATIONS | NO-GO).
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
 * @property {string[]} addressed   ids of blocking_issues the fixer applied;
 *                                  prefer namespaced ids in the form role/id
 * @property {Array<{id:string,reason:string}>} skipped
 * @property {string} new_sha       resulting SHA after fix (== input sha if no-op)
 */

/**
 * @typedef {Object} ReviewEpoch
 * @property {string} reviewed_sha
 * @property {number} cycle
 */

/**
 * @typedef {Object} Verdict
 * @property {"GO"|"NO-GO"} verdict
 * @property {string[]} reasons     human-readable explanation lines
 * @property {string[]} unaddressed_blocker_ids
 */

/**
 * Aggregate reviewer artifacts and a fix-result into a single judge verdict.
 *
 * @param {ReviewerArtifact[]} reviewers
 * @param {FixResult} fixResult
 * @param {ReviewEpoch} [expectedEpoch]
 * @returns {Verdict}
 */
export function aggregate(reviewers, fixResult, expectedEpoch = {}) {
  const reasons = [];

  if (!Array.isArray(reviewers) || reviewers.length === 0) {
    return {
      verdict: "NO-GO",
      reasons: ["No reviewer artifacts present."],
      unaddressed_blocker_ids: [],
    };
  }

  const reviewedShas = new Set(reviewers.map((r) => r.reviewed_sha));
  if (reviewedShas.size !== 1) {
    return {
      verdict: "NO-GO",
      reasons: [
        `Reviewer artifacts span multiple reviewed_sha values: ${[...reviewedShas].join(", ")}`,
      ],
      unaddressed_blocker_ids: [],
    };
  }

  const cycles = new Set(reviewers.map((r) => r.cycle));
  if (cycles.size !== 1) {
    return {
      verdict: "NO-GO",
      reasons: [`Reviewer artifacts span multiple cycle values: ${[...cycles].join(", ")}`],
      unaddressed_blocker_ids: [],
    };
  }

  const [reviewedSha] = reviewedShas;
  const [cycle] = cycles;

  if (expectedEpoch.reviewed_sha && expectedEpoch.reviewed_sha !== reviewedSha) {
    reasons.push(
      `Expected reviewed_sha ${expectedEpoch.reviewed_sha}, got reviewer artifacts for ${reviewedSha}.`
    );
  }
  if (
    Number.isInteger(expectedEpoch.cycle) &&
    expectedEpoch.cycle !== cycle
  ) {
    reasons.push(`Expected cycle ${expectedEpoch.cycle}, got reviewer artifacts for cycle ${cycle}.`);
  }

  if (!/^[0-9a-f]{40}$/.test(fixResult?.new_sha ?? "")) {
    reasons.push("Fix result is missing a valid new_sha.");
  }

  const blockerIdsByBareId = new Map();
  const knownBlockerIds = new Set();
  for (const r of reviewers) {
    for (const b of r.blocking_issues ?? []) {
      const fullId = `${r.role}/${b.id}`;
      knownBlockerIds.add(fullId);
      const ids = blockerIdsByBareId.get(b.id) ?? [];
      ids.push(fullId);
      blockerIdsByBareId.set(b.id, ids);
    }
  }

  const addressed = new Set();
  const ambiguousAddressed = [];
  const unknownAddressed = [];
  for (const id of fixResult?.addressed ?? []) {
    if (id.includes("/")) {
      if (!knownBlockerIds.has(id)) {
        unknownAddressed.push(id);
        continue;
      }
      addressed.add(id);
      continue;
    }

    const matches = blockerIdsByBareId.get(id) ?? [];
    if (matches.length === 1) {
      addressed.add(matches[0]);
      continue;
    }
    if (matches.length > 1) {
      ambiguousAddressed.push(id);
      continue;
    }
    unknownAddressed.push(id);
  }

  if (ambiguousAddressed.length > 0) {
    reasons.push(
      `Fix result addressed ambiguous blocker id(s) without reviewer namespace: ${ambiguousAddressed.join(", ")}.`
    );
  }
  if (unknownAddressed.length > 0) {
    reasons.push(
      `Fix result addressed unknown blocker id(s): ${unknownAddressed.join(", ")}.`
    );
  }
  if (addressed.size > 0 && fixResult.new_sha === reviewedSha) {
    reasons.push(
      "Fix result claims addressed blockers but new_sha matches the reviewed_sha."
    );
  }

  if (reasons.length > 0) {
    return {
      verdict: "NO-GO",
      reasons,
      unaddressed_blocker_ids: [],
    };
  }

  // Collect all unaddressed blockers across reviewers.
  const unaddressed = [];
  for (const r of reviewers) {
    for (const b of r.blocking_issues ?? []) {
      const fullId = `${r.role}/${b.id}`;
      if (!addressed.has(fullId)) {
        unaddressed.push({ role: r.role, id: b.id, severity: b.severity, message: b.message });
      }
    }
  }

  // Reviewers requesting changes whose blockers were NOT all addressed.
  const requestingChanges = reviewers.filter(
    (r) =>
      r.decision === "request_changes" &&
      (r.blocking_issues ?? []).some((b) => !addressed.has(`${r.role}/${b.id}`))
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
