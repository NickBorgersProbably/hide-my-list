#!/usr/bin/env bash
#
# reclaim-ownership.sh — give a directory back to the runner user after a
# CI agent container wrote into it.
#
# The CI image runs as `ci` (UID 1000), and agents sometimes write as root
# through the image's sudo. The runner is a different UID (1001), so it
# cannot later modify or delete those files. Later host-side steps (schema
# validation, ruff, git commit, upload-artifact) need them back.
#
# This used to be `sudo chown` on the runner. The isolated homelab runners
# (one rootless Docker daemon per repo) have no sudo at all, so the chown
# runs in a throwaway container instead. Root in that container may chown a
# bind-mounted directory to the runner's UID on rootful Docker and on
# rootless Docker alike, so this works on either kind of runner.
#
# If the image is not present locally, no agent container ran from it, so
# there is nothing to reclaim. That case exits 0 rather than pulling, so a
# failed pull does not add a second, misleading failure to the job.
#
# Usage: scripts/reclaim-ownership.sh <image-ref> <dir>

set -euo pipefail

IMAGE="${1:?usage: reclaim-ownership.sh <image-ref> <dir>}"
DIR="${2:?usage: reclaim-ownership.sh <image-ref> <dir>}"

if [ ! -d "$DIR" ]; then
  exit 0
fi
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "No local ${IMAGE}; nothing ran from it, so nothing to reclaim."
  exit 0
fi

docker run --rm --pull never --network none --user 0:0 --entrypoint chown \
  -v "${DIR}:/reclaim" "$IMAGE" -R "$(id -u):$(id -g)" /reclaim
