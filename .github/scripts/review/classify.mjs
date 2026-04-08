import { execFileSync } from "node:child_process";
import { appendFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const SPEC_MDS = new Set([
  "AGENTS.md",
  "SOUL.md",
  "TOOLS.md",
  "HEARTBEAT.md",
  "IDENTITY.md",
  "design/adhd-priorities.md",
  "docs/ai-prompts.md",
  "docs/task-lifecycle.md",
  "docs/notion-schema.md",
  "docs/architecture.md",
  "docs/user-interactions.md",
  "docs/user-preferences.md",
  "docs/reward-system.md",
  "docs/openclaw-integration.md",
]);

export function isSpecMd(file) {
  return SPEC_MDS.has(file) || file.startsWith("setup/cron/");
}

export function isReviewPromptMd(file) {
  return file.startsWith(".github/scripts/review/prompts/") && file.endsWith(".md");
}

export function isConfigOnlyPath(file) {
  if (isReviewPromptMd(file)) {
    return false;
  }

  return (
    file.startsWith(".github/") ||
    file.startsWith(".devcontainer/") ||
    file.startsWith("scripts/")
  );
}

export function isInertDoc(file) {
  return file === "README.md" || file.endsWith(".md");
}

export function requiresSecurityReview(file) {
  return (
    file.startsWith(".github/workflows/") ||
    file.startsWith(".github/actions/") ||
    file.startsWith(".github/scripts/review/") ||
    file.startsWith("scripts/")
  );
}

export function classifyFiles(files) {
  let docsOnly = true;
  let configOnly = true;
  let securityForced = false;

  for (const file of files.filter(Boolean)) {
    if (requiresSecurityReview(file)) {
      securityForced = true;
    }

    if (!isConfigOnlyPath(file)) {
      configOnly = false;
    }

    if ((isSpecMd(file) || isReviewPromptMd(file)) || !isInertDoc(file)) {
      docsOnly = false;
    }
  }

  const roles = ["design", "docs"];
  if (securityForced || !docsOnly) {
    roles.push("security");
  }
  if (!configOnly) {
    roles.push("psych", "prompt");
  }

  return {
    roles,
    rolesJson: JSON.stringify(roles),
    docsOnly,
    configOnly,
  };
}

export function getChangedFiles({ baseSha = "", baseRef = "", headSha, cwd = process.cwd() }) {
  if (!headSha) {
    throw new Error("review-classify: HEAD_SHA is required");
  }

  const diffArgs = ["diff", "--name-only"];
  if (baseSha) {
    diffArgs.push(baseSha, headSha);
  } else if (baseRef) {
    diffArgs.push(`${baseRef}...${headSha}`);
  } else {
    throw new Error("review-classify: BASE_SHA or BASE_REF is required");
  }

  const stdout = execFileSync("git", diffArgs, {
    cwd,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });

  return stdout
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function emitOutputs({ rolesJson, docsOnly, configOnly }, githubOutput) {
  if (!githubOutput) {
    return;
  }

  appendFileSync(
    githubOutput,
    `roles_json=${rolesJson}\ndocs_only=${docsOnly}\nconfig_only=${configOnly}\n`,
    "utf8"
  );
}

function main() {
  const { BASE_SHA = "", BASE_REF = "", HEAD_SHA = "", GITHUB_OUTPUT = "" } = process.env;
  const files = getChangedFiles({ baseSha: BASE_SHA, baseRef: BASE_REF, headSha: HEAD_SHA });
  const result = classifyFiles(files);

  if (files.length === 0) {
    console.log(`review-classify: empty diff for ${HEAD_SHA}`);
  }

  emitOutputs(result, GITHUB_OUTPUT);
  console.log(
    `review-classify: roles=${result.rolesJson} docs_only=${result.docsOnly} config_only=${result.configOnly}`
  );
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main();
}
