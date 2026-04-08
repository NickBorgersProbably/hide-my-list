#!/usr/bin/env bash
# Guard against regressions in the PR Tests workflow where workflow
# validation invokes actionlint-dependent checks before actionlint is
# installed on the runner.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"

python3 - "$REPO_ROOT" <<'PY'
import sys
from pathlib import Path

try:
    import yaml
except ImportError as exc:
    print(f"ERROR: PyYAML is required to validate pr-tests workflow ordering: {exc}")
    sys.exit(1)


repo_root = Path(sys.argv[1])
workflow_path = repo_root / ".github" / "workflows" / "pr-tests.yml"

try:
    data = yaml.safe_load(workflow_path.read_text(encoding="utf-8")) or {}
except yaml.YAMLError as exc:
    print(f"ERROR: failed to parse {workflow_path.relative_to(repo_root)}: {exc}")
    sys.exit(1)

jobs = data.get("jobs")
if not isinstance(jobs, dict):
    print("ERROR: pr-tests.yml must define jobs")
    sys.exit(1)

job = jobs.get("workflow-validation")
if not isinstance(job, dict):
    print("ERROR: pr-tests.yml is missing the workflow-validation job")
    sys.exit(1)

steps = job.get("steps")
if not isinstance(steps, list):
    print("ERROR: workflow-validation job must define a steps list")
    sys.exit(1)

install_index = None
errors = []


def uses_actionlint(run_block: str) -> bool:
    return "./scripts/run-required-checks.sh ci-workflows" in run_block or "actionlint " in run_block


for index, step in enumerate(steps):
    if not isinstance(step, dict):
        continue

    name = step.get("name", f"step #{index + 1}")
    run_block = step.get("run")

    if isinstance(name, str) and name == "Install actionlint":
        install_index = index

    if not isinstance(run_block, str) or not uses_actionlint(run_block):
        continue

    if install_index is None:
        errors.append(
            f"ERROR: workflow-validation step '{name}' uses actionlint before any 'Install actionlint' step."
        )
    elif index < install_index:
        errors.append(
            f"ERROR: workflow-validation step '{name}' appears before 'Install actionlint'."
        )


print("=== Checking PR Tests workflow validation step order ===")

if errors:
    for error in errors:
        print(error)
    print(f"FAILED: {len(errors)} pr-tests workflow ordering error(s) found")
    sys.exit(1)

print("PR Tests workflow installs actionlint before actionlint-dependent validation")
PY
