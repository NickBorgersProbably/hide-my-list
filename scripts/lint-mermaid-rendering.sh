#!/usr/bin/env bash
# Lint Mermaid diagrams for known rendering issues that mermaid.parse() won't catch.
# Checks for over-escaped HTML entities inside mermaid code blocks.
#
# Usage: lint-mermaid-rendering.sh [file-or-dir...]
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

# Check a single markdown file for rendering issues in mermaid blocks
check_file() {
  local mdfile="$1"
  local in_mermaid=0
  local line_num=0
  local block_num=0

  while IFS= read -r line; do
    line_num=$((line_num + 1))

    if [[ "$line" =~ ^\`\`\`mermaid ]]; then
      in_mermaid=1
      block_num=$((block_num + 1))
      continue
    fi

    if [[ $in_mermaid -eq 1 && "$line" =~ ^\`\`\` ]]; then
      in_mermaid=0
      continue
    fi

    if [[ $in_mermaid -eq 1 ]]; then
      # Check for over-escaped HTML angle brackets
      # #lt; and #gt; indicate double-escaping (should be <br/> not #lt;br/#gt;)
      if echo "$line" | grep -qE '#lt;|#gt;'; then
        echo "ERROR: Over-escaped HTML entity in $mdfile:$line_num (mermaid block #$block_num)"
        echo "  $line"
        echo "  Hint: Use <br/> directly, not #lt;br/#gt;"
        echo ""
        ERRORS=$((ERRORS + 1))
      fi
    fi
  done < "$mdfile"

  if [[ $block_num -gt 0 ]]; then
    CHECKED=$((CHECKED + block_num))
  fi
}

# Process individual files passed as arguments
for mdfile in "${FILES[@]}"; do
  [[ "$mdfile" == *.md ]] || continue
  check_file "$mdfile"
done

# Find markdown files in directories
for dir in "${DIRS[@]}"; do
  [ -d "$dir" ] || continue
  while IFS= read -r -d '' mdfile; do
    check_file "$mdfile"
  done < <(find "$dir" -name '*.md' -print0)
done

echo "Checked $CHECKED Mermaid block(s) for rendering issues"
if [ "$ERRORS" -gt 0 ]; then
  echo "$ERRORS rendering issue(s) found"
  exit 1
fi
echo "No rendering issues found"
