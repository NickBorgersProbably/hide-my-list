// Unit tests for the v2 review pipeline judge logic.
//
// Run with: node --test .github/scripts/review/aggregate.test.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import { aggregate } from "./aggregate.mjs";

const SHA = "a".repeat(40);
const NEXT_SHA = "b".repeat(40);

/** Helper: build a minimal reviewer artifact. */
function reviewer(role, decision, blockers = [], overrides = {}) {
  return {
    schema_version: "1",
    role,
    reviewed_sha: SHA,
    cycle: 1,
    decision,
    summary: "",
    blocking_issues: blockers,
    non_blocking_notes: [],
    fix_suggestions: [],
    followup_issues: [],
    ...overrides,
  };
}

function blocker(id, severity = "high") {
  return { id, severity, message: `issue ${id}` };
}

const noFix = { addressed: [], skipped: [], new_sha: SHA };
const epoch = { reviewed_sha: SHA, cycle: 1 };

test("empty reviewer set is NO-GO", () => {
  const v = aggregate([], noFix, epoch);
  assert.equal(v.verdict, "NO-GO");
});

test("all approve, no blockers -> GO", () => {
  const v = aggregate(
    [reviewer("design", "approve"), reviewer("security", "approve")],
    noFix,
    epoch
  );
  assert.equal(v.verdict, "GO");
  assert.deepEqual(v.unaddressed_blocker_ids, []);
});

test("approve + comment + abstain, no blockers -> GO", () => {
  const v = aggregate(
    [
      reviewer("design", "approve"),
      reviewer("security", "comment"),
      reviewer("psych", "abstain"),
    ],
    noFix,
    epoch
  );
  assert.equal(v.verdict, "GO");
});

test("all abstain -> GO (vacuous)", () => {
  const v = aggregate(
    [reviewer("design", "abstain"), reviewer("security", "abstain")],
    noFix,
    epoch
  );
  assert.equal(v.verdict, "GO");
  assert.match(v.reasons.join("\n"), /vacuous/i);
});

test("unaddressed critical blocker -> NO-GO", () => {
  const v = aggregate(
    [reviewer("security", "request_changes", [blocker("sec-1", "critical")])],
    noFix,
    epoch
  );
  assert.equal(v.verdict, "NO-GO");
  assert.deepEqual(v.unaddressed_blocker_ids, ["security/sec-1"]);
});

test("unaddressed medium blocker -> NO-GO", () => {
  const v = aggregate(
    [reviewer("design", "request_changes", [blocker("d-1", "medium")])],
    noFix,
    epoch
  );
  assert.equal(v.verdict, "NO-GO");
});

test("blocker fully addressed by fixer -> GO", () => {
  const v = aggregate(
    [reviewer("security", "request_changes", [blocker("sec-1")])],
    { addressed: ["security/sec-1"], skipped: [], new_sha: NEXT_SHA },
    epoch
  );
  assert.equal(v.verdict, "GO");
});

test("partially addressed blockers -> NO-GO with remaining ids", () => {
  const v = aggregate(
    [
      reviewer("security", "request_changes", [
        blocker("sec-1"),
        blocker("sec-2"),
      ]),
      reviewer("design", "request_changes", [blocker("d-1")]),
    ],
    {
      addressed: ["security/sec-1"],
      skipped: [{ id: "security/sec-2", reason: "manual" }],
      new_sha: NEXT_SHA,
    },
    epoch
  );
  assert.equal(v.verdict, "NO-GO");
  assert.deepEqual(
    v.unaddressed_blocker_ids.sort(),
    ["design/d-1", "security/sec-2"]
  );
});

test("request_changes with all blockers addressed -> GO", () => {
  // Edge case: a reviewer can return decision=request_changes but the
  // fixer addressed every blocker. We treat the fix as authoritative.
  const v = aggregate(
    [reviewer("security", "request_changes", [blocker("sec-1")])],
    { addressed: ["security/sec-1"], skipped: [], new_sha: "c".repeat(40) },
    epoch
  );
  assert.equal(v.verdict, "GO");
});

test("verdict object always carries reasons", () => {
  const v = aggregate([reviewer("design", "approve")], noFix, epoch);
  assert.ok(Array.isArray(v.reasons));
  assert.ok(v.reasons.length > 0);
});

test("mixed reviewed_sha reviewer artifacts fail closed", () => {
  const v = aggregate(
    [
      reviewer("design", "approve"),
      reviewer("security", "approve", [], { reviewed_sha: NEXT_SHA }),
    ],
    noFix,
    epoch
  );
  assert.equal(v.verdict, "NO-GO");
  assert.match(v.reasons.join("\n"), /multiple reviewed_sha/i);
});

test("mixed review cycles fail closed", () => {
  const v = aggregate(
    [
      reviewer("design", "approve"),
      reviewer("security", "approve", [], { cycle: 2 }),
    ],
    noFix,
    epoch
  );
  assert.equal(v.verdict, "NO-GO");
  assert.match(v.reasons.join("\n"), /multiple cycle/i);
});

test("expected epoch mismatch fails closed", () => {
  const v = aggregate(
    [reviewer("design", "approve")],
    noFix,
    { reviewed_sha: NEXT_SHA, cycle: 2 }
  );
  assert.equal(v.verdict, "NO-GO");
  assert.match(v.reasons.join("\n"), /Expected reviewed_sha/);
  assert.match(v.reasons.join("\n"), /Expected cycle/);
});

test("duplicate blocker ids across reviewers require namespaced addressed ids", () => {
  const v = aggregate(
    [
      reviewer("design", "request_changes", [blocker("shared-1")]),
      reviewer("security", "request_changes", [blocker("shared-1")]),
    ],
    { addressed: ["shared-1"], skipped: [], new_sha: NEXT_SHA },
    epoch
  );
  assert.equal(v.verdict, "NO-GO");
  assert.match(v.reasons.join("\n"), /ambiguous blocker id/i);
});

test("duplicate blocker ids across reviewers only clear the namespaced blocker", () => {
  const v = aggregate(
    [
      reviewer("design", "request_changes", [blocker("shared-1")]),
      reviewer("security", "request_changes", [blocker("shared-1")]),
    ],
    { addressed: ["design/shared-1"], skipped: [], new_sha: NEXT_SHA },
    epoch
  );
  assert.equal(v.verdict, "NO-GO");
  assert.deepEqual(v.unaddressed_blocker_ids, ["security/shared-1"]);
});

test("unknown namespaced addressed ids fail closed", () => {
  const v = aggregate(
    [reviewer("security", "request_changes", [blocker("sec-1")])],
    { addressed: ["security/sec-404"], skipped: [], new_sha: NEXT_SHA },
    epoch
  );
  assert.equal(v.verdict, "NO-GO");
  assert.match(v.reasons.join("\n"), /unknown blocker id/i);
});

test("addressed blockers require a new sha", () => {
  const v = aggregate(
    [reviewer("security", "request_changes", [blocker("sec-1")])],
    { addressed: ["security/sec-1"], skipped: [], new_sha: SHA },
    epoch
  );
  assert.equal(v.verdict, "NO-GO");
  assert.match(v.reasons.join("\n"), /new_sha matches the reviewed_sha/i);
});
