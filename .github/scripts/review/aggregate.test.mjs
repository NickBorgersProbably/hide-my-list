// Unit tests for the v2 review pipeline judge logic.
//
// Run with: node --test .github/scripts/review/aggregate.test.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import { aggregate } from "./aggregate.mjs";

const SHA = "a".repeat(40);

/** Helper: build a minimal reviewer artifact. */
function reviewer(role, decision, blockers = []) {
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
  };
}

function blocker(id, severity = "high") {
  return { id, severity, message: `issue ${id}` };
}

const noFix = { addressed: [], skipped: [], new_sha: SHA };

test("empty reviewer set is NO-GO", () => {
  const v = aggregate([], noFix);
  assert.equal(v.verdict, "NO-GO");
});

test("all approve, no blockers -> GO", () => {
  const v = aggregate(
    [reviewer("design", "approve"), reviewer("security", "approve")],
    noFix
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
    noFix
  );
  assert.equal(v.verdict, "GO");
});

test("all abstain -> GO (vacuous)", () => {
  const v = aggregate(
    [reviewer("design", "abstain"), reviewer("security", "abstain")],
    noFix
  );
  assert.equal(v.verdict, "GO");
  assert.match(v.reasons.join("\n"), /vacuous/i);
});

test("unaddressed critical blocker -> NO-GO", () => {
  const v = aggregate(
    [reviewer("security", "request_changes", [blocker("sec-1", "critical")])],
    noFix
  );
  assert.equal(v.verdict, "NO-GO");
  assert.deepEqual(v.unaddressed_blocker_ids, ["security/sec-1"]);
});

test("unaddressed medium blocker -> NO-GO", () => {
  const v = aggregate(
    [reviewer("design", "request_changes", [blocker("d-1", "medium")])],
    noFix
  );
  assert.equal(v.verdict, "NO-GO");
});

test("blocker fully addressed by fixer -> GO", () => {
  const v = aggregate(
    [reviewer("security", "request_changes", [blocker("sec-1")])],
    { addressed: ["sec-1"], skipped: [], new_sha: "b".repeat(40) }
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
    { addressed: ["sec-1"], skipped: [{ id: "sec-2", reason: "manual" }], new_sha: SHA }
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
    { addressed: ["sec-1"], skipped: [], new_sha: "c".repeat(40) }
  );
  assert.equal(v.verdict, "GO");
});

test("verdict object always carries reasons", () => {
  const v = aggregate([reviewer("design", "approve")], noFix);
  assert.ok(Array.isArray(v.reasons));
  assert.ok(v.reasons.length > 0);
});
