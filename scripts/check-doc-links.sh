#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

python3 - "$REPO_ROOT" <<'PY'
import re
import sys
from pathlib import Path

repo_root = Path(sys.argv[1]).resolve()
scan_targets = [
    repo_root / "docs",
    repo_root / "design",
    repo_root / "setup",
    repo_root / "README.md",
    repo_root / "AGENTS.md",
]
link_pattern = re.compile(r"\[[^\]]*]\(([^)]+)\)")
files = []
errors = []

for target in scan_targets:
    if target.is_dir():
        files.extend(sorted(target.rglob("*.md")))
    elif target.is_file():
        files.append(target)

for file_path in files:
    content = file_path.read_text(encoding="utf-8")
    for raw_link in link_pattern.findall(content):
        link = raw_link.strip()
        if not link or link.startswith(("http://", "https://", "mailto:", "tel:", "#")):
            continue

        target = link.split("#", 1)[0].strip()
        if not target:
            continue

        resolved = (file_path.parent / target).resolve()
        if not resolved.exists():
            try:
                display_target = resolved.relative_to(repo_root)
            except ValueError:
                display_target = resolved
            errors.append(
                f"BROKEN LINK in {file_path.relative_to(repo_root)}: "
                f"{raw_link} (resolved to {display_target})"
            )

if errors:
    for error in errors:
        print(error)
    print(f"{len(errors)} broken internal link(s) found")
    sys.exit(1)

print("No broken internal links found")
PY
