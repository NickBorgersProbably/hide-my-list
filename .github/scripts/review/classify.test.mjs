import { execFileSync } from "node:child_process";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { classifyFiles, getChangedFiles } from "./classify.mjs";

function initRepo() {
  const cwd = mkdtempSync(join(tmpdir(), "review-classify-"));
  execFileSync("git", ["init", "-b", "main"], { cwd, stdio: "ignore" });
  execFileSync("git", ["config", "user.name", "Codex"], { cwd, stdio: "ignore" });
  execFileSync("git", ["config", "user.email", "codex@example.com"], {
    cwd,
    stdio: "ignore",
  });

  mkdirSync(join(cwd, ".github", "workflows"), { recursive: true });
  writeFileSync(join(cwd, ".github", "workflows", "review-pipeline.yml"), "name: review\n");
  execFileSync("git", ["add", "."], { cwd, stdio: "ignore" });
  execFileSync("git", ["commit", "-m", "base"], { cwd, stdio: "ignore" });

  const baseSha = execFileSync("git", ["rev-parse", "HEAD"], {
    cwd,
    encoding: "utf8",
  }).trim();

  writeFileSync(
    join(cwd, ".github", "workflows", "review-pipeline.yml"),
    "name: review\npermissions:\n  contents: read\n"
  );
  execFileSync("git", ["add", "."], { cwd, stdio: "ignore" });
  execFileSync("git", ["commit", "-m", "change workflow"], { cwd, stdio: "ignore" });

  const headSha = execFileSync("git", ["rev-parse", "HEAD"], {
    cwd,
    encoding: "utf8",
  }).trim();

  return { cwd, baseSha, headSha };
}

test("workflow orchestration files force security review", () => {
  const result = classifyFiles([".github/workflows/review-fixer.yml"]);
  assert.deepEqual(result.roles, ["design", "docs", "security"]);
  assert.equal(result.docsOnly, false);
  assert.equal(result.configOnly, true);
});

test("workflow permission changes still force security review", () => {
  const result = classifyFiles([".github/workflows/review-pipeline.yml"]);
  assert.deepEqual(result.roles, ["design", "docs", "security"]);
});

test("local action changes force security review", () => {
  const result = classifyFiles([".github/actions/review-publish/action.yml"]);
  assert.deepEqual(result.roles, ["design", "docs", "security"]);
});

test("review prompt markdown keeps docs/prompt routing and also includes security", () => {
  const result = classifyFiles([".github/scripts/review/prompts/security.md"]);
  assert.deepEqual(result.roles, ["design", "docs", "security", "psych", "prompt"]);
  assert.equal(result.docsOnly, false);
  assert.equal(result.configOnly, false);
});

test("runtime scripts force security review", () => {
  const result = classifyFiles(["scripts/notion-cli.sh"]);
  assert.deepEqual(result.roles, ["design", "docs", "security"]);
});

test("inert docs stay docs-only but still route psych/prompt", () => {
  const result = classifyFiles(["docs/faq.md", "README.md"]);
  assert.deepEqual(result.roles, ["design", "docs", "psych", "prompt"]);
  assert.equal(result.docsOnly, true);
  assert.equal(result.configOnly, false);
});

test("SHA-bound diff returns changed files for workflow changes", (t) => {
  const { cwd, baseSha, headSha } = initRepo();
  t.after(() => rmSync(cwd, { recursive: true, force: true }));

  const files = getChangedFiles({ cwd, baseSha, headSha });
  assert.deepEqual(files, [".github/workflows/review-pipeline.yml"]);
});
