#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <ci-image-ref>" >&2
  exit 64
fi

CI_IMAGE="$1"
REPO_ROOT="$(git rev-parse --show-toplevel)"
VERSIONS_FILE="${REPO_ROOT}/.github/ci/versions.env"
DOCKERFILE="${REPO_ROOT}/.github/ci/Dockerfile"

if [ ! -f "$VERSIONS_FILE" ]; then
  echo "ERROR: missing versions file at ${VERSIONS_FILE}" >&2
  exit 1
fi

if [ ! -f "$DOCKERFILE" ]; then
  echo "ERROR: missing CI Dockerfile at ${DOCKERFILE}" >&2
  exit 1
fi

if docker pull "$CI_IMAGE"; then
  echo "Using published CI image: ${CI_IMAGE}"
  exit 0
fi

echo "::warning::Unable to pull ${CI_IMAGE}; building a local fallback from .github/ci/Dockerfile"

set -a
# shellcheck disable=SC1090
source <(grep -Ev '^[[:space:]]*(#|$)' "$VERSIONS_FILE")
set +a

docker build \
  --file "$DOCKERFILE" \
  --tag "$CI_IMAGE" \
  --build-arg "CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION}" \
  --build-arg "CODEX_CLI_VERSION=${CODEX_CLI_VERSION}" \
  --build-arg "ACTIONLINT_VERSION=${ACTIONLINT_VERSION}" \
  --build-arg "NODE_MAJOR=${NODE_MAJOR}" \
  "$REPO_ROOT"

echo "Built local fallback CI image: ${CI_IMAGE}"
