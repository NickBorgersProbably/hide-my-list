#!/usr/bin/env node
// Judge driver for the v2 review pipeline.
//
// Reads reviewer artifacts from REVIEWER_DIR and the fix-result from
// FIX_RESULT_PATH, calls aggregate(), writes verdict-<sha>.json to
// VERDICT_OUTPUT_PATH, and prints `verdict=<GO|NO-GO>` to stdout
// (also appends to GITHUB_OUTPUT if set).
//
// Pure file IO + a single aggregate() call. No git, no API, no Codex.
// Designed to run in a job with permissions: contents: read so the
// judge structurally cannot push.

import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { aggregate } from "./aggregate.mjs";

const REVIEWER_DIR = process.env.REVIEWER_DIR;
const FIX_RESULT_PATH = process.env.FIX_RESULT_PATH;
const VERDICT_OUTPUT_PATH = process.env.VERDICT_OUTPUT_PATH;

if (!REVIEWER_DIR || !FIX_RESULT_PATH || !VERDICT_OUTPUT_PATH) {
  console.error(
    "judge.mjs: REVIEWER_DIR, FIX_RESULT_PATH, and VERDICT_OUTPUT_PATH must be set"
  );
  process.exit(2);
}

// Reviewer artifacts are downloaded as `reviewer-<role>-<sha>/<role>-result.json`.
// Walk one level down to find them.
const reviewers = [];
for (const entry of readdirSync(REVIEWER_DIR, { withFileTypes: true })) {
  if (!entry.isDirectory()) continue;
  const dir = join(REVIEWER_DIR, entry.name);
  const candidate = readdirSync(dir).find((f) => f.endsWith("-result.json"));
  if (!candidate) continue;
  reviewers.push(JSON.parse(readFileSync(join(dir, candidate), "utf8")));
}

const fixResult = JSON.parse(readFileSync(FIX_RESULT_PATH, "utf8"));

const verdict = aggregate(reviewers, fixResult);

console.log(JSON.stringify(verdict, null, 2));
writeFileSync(VERDICT_OUTPUT_PATH, JSON.stringify(verdict, null, 2) + "\n");

// Emit GitHub Actions step output if running inside Actions.
const ghOut = process.env.GITHUB_OUTPUT;
if (ghOut) {
  writeFileSync(ghOut, `verdict=${verdict.verdict}\n`, { flag: "a" });
}
