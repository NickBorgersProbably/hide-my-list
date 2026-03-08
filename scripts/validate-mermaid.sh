#!/usr/bin/env bash
# Validate Mermaid diagrams in markdown files.
# Extracts ```mermaid code blocks and validates syntax using the Mermaid JS parser
# with a JSDOM environment (mermaid requires DOM APIs even for parse-only mode).
# This catches errors that would prevent GitHub from rendering diagrams.
#
# Dependencies (mermaid, jsdom, dompurify) are pre-installed in the devcontainer.
# Falls back to installing them in a temp directory if not found globally.
#
# Usage: validate-mermaid.sh [file-or-dir...]
# Accepts markdown files and/or directories (searched recursively).
# Defaults to docs/ and design/ if no arguments specified.

set -euo pipefail

ARGS=()
if [ $# -gt 0 ]; then
  ARGS=("$@")
else
  ARGS=(docs/ design/)
fi

# Separate files and directories
FILES=()
DIRS=()
for arg in "${ARGS[@]}"; do
  if [ -f "$arg" ]; then
    FILES+=("$arg")
  elif [ -d "$arg" ]; then
    DIRS+=("$arg")
  else
    echo "WARNING: $arg is not a file or directory, skipping"
  fi
done

ERRORS=0
CHECKED=0
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

# Create the Node.js validator (uses JSDOM to satisfy mermaid's DOM requirements)
cat > "$TMPDIR/validate.mjs" << 'VALIDATOR'
import { JSDOM } from "jsdom";

// Set up minimal DOM environment before importing mermaid
const dom = new JSDOM("<!DOCTYPE html><html><body></body></html>");
global.window = dom.window;
global.document = dom.window.document;
global.navigator = dom.window.navigator;
global.DOMPurify = (await import("dompurify")).default(dom.window);

const { default: mermaid } = await import("mermaid");
mermaid.initialize({ startOnLoad: false });

const fs = await import("fs");
const file = process.argv[2];
const content = fs.readFileSync(file, "utf8");

try {
  await mermaid.parse(content);
  process.exit(0);
} catch (e) {
  console.error(e.message || e);
  process.exit(1);
}
VALIDATOR

# Check if dependencies are available globally (pre-installed in devcontainer/CI).
# ESM imports don't use NODE_PATH, so we symlink global node_modules into the temp dir.
GLOBAL_MODULES=$(npm root -g 2>/dev/null)
if [ -n "$GLOBAL_MODULES" ] && \
   [ -d "$GLOBAL_MODULES/mermaid" ] && [ -d "$GLOBAL_MODULES/jsdom" ] && [ -d "$GLOBAL_MODULES/dompurify" ]; then
  echo "Using pre-installed Mermaid dependencies"
  ln -s "$GLOBAL_MODULES" "$TMPDIR/node_modules"
else
  echo "Installing Mermaid dependencies..."
  (cd "$TMPDIR" && npm init -y --silent > /dev/null 2>&1 && npm install mermaid@11 jsdom@25 dompurify@3 > /dev/null 2>&1)
fi

# Validate mermaid blocks in a single markdown file
validate_file() {
  local mdfile="$1"

  # Extract mermaid blocks with awk
  awk '
    /^```mermaid/ { capture=1; block++; next }
    /^```/ && capture { capture=0; next }
    capture { print > "'"$TMPDIR"'/block_" block ".mmd" }
  ' "$mdfile"

  # Validate each extracted block
  for block_file in "$TMPDIR"/block_*.mmd; do
    [ -f "$block_file" ] || continue
    CHECKED=$((CHECKED + 1))
    BLOCK_NUM=$(basename "$block_file" | sed 's/block_//;s/\.mmd//')

    if ! node "$TMPDIR/validate.mjs" "$block_file" 2>"$TMPDIR/err.txt"; then
      echo "ERROR: Invalid Mermaid diagram in $mdfile (block #$BLOCK_NUM)"
      echo "  Content (first 5 lines):"
      head -5 "$block_file" | sed 's/^/    /'
      echo "  Error:"
      sed 's/^/    /' "$TMPDIR/err.txt"
      echo ""
      ERRORS=$((ERRORS + 1))
    fi
    rm -f "$block_file" "$TMPDIR/err.txt"
  done
}

# Process individual files passed as arguments
for mdfile in "${FILES[@]}"; do
  [[ "$mdfile" == *.md ]] || continue
  validate_file "$mdfile"
done

# Find markdown files in directories
for dir in "${DIRS[@]}"; do
  [ -d "$dir" ] || continue
  while IFS= read -r -d '' mdfile; do
    validate_file "$mdfile"
  done < <(find "$dir" -name '*.md' -print0)
done

echo "Checked $CHECKED Mermaid diagram(s)"
if [ "$ERRORS" -gt 0 ]; then
  echo "$ERRORS diagram(s) have syntax errors that will not render on GitHub"
  exit 1
fi
echo "All diagrams are valid"
