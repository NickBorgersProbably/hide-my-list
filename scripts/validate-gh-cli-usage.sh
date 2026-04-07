#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"

python3 - "$REPO_ROOT" <<'PY'
import sys
import re
from pathlib import Path


repo_root = Path(sys.argv[1])
targets = []
targets.extend(sorted((repo_root / ".github" / "workflows").glob("*.yml")))
targets.extend(sorted((repo_root / ".github" / "workflows").glob("*.yaml")))

errors = []
command_pattern = re.compile(r"(^|[\s(])gh\s+api\b")

for path in targets:
    lines = path.read_text(encoding="utf-8").splitlines()
    line_count = len(lines)
    idx = 0

    while idx < line_count:
        start = idx
        command_lines = [lines[idx]]
        while command_lines[-1].rstrip().endswith("\\") and idx + 1 < line_count:
            idx += 1
            command_lines.append(lines[idx])

        command = " ".join(part.strip().rstrip("\\") for part in command_lines)
        if not command_pattern.search(command):
            idx += 1
            continue

        if "--slurp" in command and ("--jq" in command or "--template" in command):
            errors.append(
                f"ERROR: {path.relative_to(repo_root)}:{start + 1}: "
                f"`gh api` cannot combine `--slurp` with `--jq` or `--template`: {command}"
            )

        idx += 1

print("=== Checking gh api flag compatibility ===")

if errors:
    for error in errors:
        print(error)
    print(f"FAILED: {len(errors)} invalid gh api command(s) found")
    sys.exit(1)

print("All gh api invocations use supported flag combinations")
PY
