#!/usr/bin/env bash
# load-env.sh — source a narrowed subset of .env into the current shell
#
# Usage:
#   source load-env.sh NOTION_API_KEY NOTION_DATABASE_ID
#   source load-env.sh GITHUB_PAT?
#
# Variables ending in ? are optional. Required variables must already exist in
# the current environment or be present in .env. The helper only exports the
# named variables into the caller shell, so scripts do not absorb unrelated
# credentials from the shared .env file.

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    echo "load-env.sh must be sourced, not executed" >&2
    exit 1
fi

if [ "$#" -eq 0 ]; then
    echo "usage: source load-env.sh VAR_NAME [OPTIONAL_VAR? ...]" >&2
    return 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${HIDE_MY_LIST_ENV_FILE:-$SCRIPT_DIR/../.env}"
VALID_NAME_REGEX='^[A-Za-z_][A-Za-z0-9_]*$'

missing_specs=()
needs_env_file=0

for spec in "$@"; do
    name="${spec%\?}"
    if [[ ! "$name" =~ $VALID_NAME_REGEX ]]; then
        echo "invalid env var name: $name" >&2
        return 1
    fi

    if [ "${!name+x}" = x ]; then
        export "${name?}"
        continue
    fi

    missing_specs+=("$spec")
    if [ "$spec" = "$name" ]; then
        needs_env_file=1
    fi
done

if [ "${#missing_specs[@]}" -eq 0 ]; then
    return 0
fi

if [ ! -f "$ENV_FILE" ]; then
    if [ "$needs_env_file" -eq 0 ]; then
        return 0
    fi
    echo "env file not found: $ENV_FILE" >&2
    return 1
fi

loader_output="$(
    bash -s -- "$ENV_FILE" "${missing_specs[@]}" <<'BASH'
set -euo pipefail

env_file=$1
shift

set -a
# shellcheck source=/dev/null
source "$env_file"

python3 - "$@" <<'PY'
import os
import re
import shlex
import sys

valid_name = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

for spec in sys.argv[1:]:
    optional = spec.endswith("?")
    name = spec[:-1] if optional else spec

    if not valid_name.match(name):
        print(f"invalid env var name: {name}", file=sys.stderr)
        raise SystemExit(1)

    if name in os.environ:
        print(f"declare -x {name}={shlex.quote(os.environ[name])}")
    elif not optional:
        print(f"missing required env var: {name}", file=sys.stderr)
        raise SystemExit(1)
PY
BASH
)" || return $?

eval "$loader_output"
