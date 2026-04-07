#!/usr/bin/env bash
# Guard against `gh api --slurp` combined with `--jq` or `--template`.
# gh CLI rejects that combination at runtime, which has broken workflows
# before (see issue #331). This check fails fast at PR time instead.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

shopt -s nullglob globstar

files=(
  .github/workflows/*.yml
  .github/workflows/*.yaml
  .github/actions/**/*.yml
  .github/actions/**/*.yaml
)

status=0
for f in "${files[@]}"; do
  # Join backslash-continued lines so a multi-line `gh api ...` invocation
  # becomes one logical line. Emit "<starting-lineno>\t<joined-line>".
  while IFS=$'\t' read -r lineno joined; do
    case "$joined" in
      *"gh api"*"--slurp"*)
        if [[ "$joined" == *"--jq"* || "$joined" == *"--template"* ]]; then
          echo "ERROR: $f:$lineno: \`gh api --slurp\` cannot be combined with \`--jq\` or \`--template\`."
          echo "       Use \`gh api --paginate | jq -s '...'\` instead."
          status=1
        fi
        ;;
    esac
  done < <(awk '
    {
      if (buf == "") start = NR
      line = $0
      sub(/[[:space:]]*$/, "", line)
      if (line ~ /\\$/) {
        sub(/\\$/, "", line)
        buf = buf line
        next
      }
      print start "\t" buf line
      buf = ""
    }
    END { if (buf != "") print start "\t" buf }
  ' "$f")
done

if [ "$status" -ne 0 ]; then
  exit 1
fi
echo "OK: no invalid \`gh api --slurp\` combinations found."
