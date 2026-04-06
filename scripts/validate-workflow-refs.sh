#!/usr/bin/env bash
set -euo pipefail

# Validates cross-file references in GitHub Actions workflows:
# 1. Local composite action references (uses: ./.github/actions/X) resolve to real directories
# 2. workflow_run trigger names match actual workflow name: fields
# 3. Composite action directories contain action.yml

REPO_ROOT="$(git rev-parse --show-toplevel)"

python3 - "$REPO_ROOT" <<'PY'
import sys
from pathlib import Path

try:
    import yaml
except ImportError as exc:
    print(f"ERROR: PyYAML is required to validate workflow references: {exc}")
    sys.exit(1)


repo_root = Path(sys.argv[1])
workflow_dir = repo_root / ".github" / "workflows"
workflow_files = sorted(workflow_dir.glob("*.yml")) + sorted(workflow_dir.glob("*.yaml"))
errors = []
workflows = []
workflow_names = set()


def load_workflow(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        errors.append(f"ERROR: {path.name}: failed to parse YAML: {exc}")
        return {}

    if not isinstance(data, dict):
        errors.append(f"ERROR: {path.name}: workflow file must parse to a top-level mapping")
        return {}

    return data


def iter_uses(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "uses" and isinstance(value, str):
                yield value
            yield from iter_uses(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_uses(item)


print("=== Checking local composite action references ===")

for workflow_file in workflow_files:
    data = load_workflow(workflow_file)
    workflows.append((workflow_file, data))

    name = data.get("name")
    if isinstance(name, str) and name.strip():
        workflow_names.add(name.strip())

    for action_path in iter_uses(data):
        if not action_path.startswith("./.github/actions/"):
            continue

        action_dir = repo_root / action_path[2:]
        if not action_dir.is_dir():
            errors.append(
                f"ERROR: {workflow_file.name}: references action '{action_path}' but directory does not exist"
            )
            continue

        if not (action_dir / "action.yml").is_file() and not (action_dir / "action.yaml").is_file():
            errors.append(
                f"ERROR: {workflow_file.name}: references action '{action_path}' but no action.yml found in directory"
            )


print("=== Checking workflow_run trigger name consistency ===")

for workflow_file, data in workflows:
    on_section = data.get("on", data.get(True))
    if not isinstance(on_section, dict):
        continue

    workflow_run = on_section.get("workflow_run")
    if not isinstance(workflow_run, dict):
        continue

    referenced = workflow_run.get("workflows", [])
    if isinstance(referenced, str):
        referenced = [referenced]
    elif not isinstance(referenced, list):
        errors.append(
            f"ERROR: {workflow_file.name}: workflow_run.workflows must be a string or list of strings"
        )
        continue

    for ref_name in referenced:
        if not isinstance(ref_name, str):
            errors.append(
                f"ERROR: {workflow_file.name}: workflow_run.workflows contains a non-string entry"
            )
            continue

        normalized = ref_name.strip()
        if normalized and normalized not in workflow_names:
            errors.append(
                f"ERROR: {workflow_file.name}: workflow_run references '{normalized}' but no workflow with that name exists"
            )


print("")
if errors:
    for error in errors:
        print(error)
    print(f"FAILED: {len(errors)} cross-file reference error(s) found")
    sys.exit(1)

print("All cross-file workflow references are valid")
PY
