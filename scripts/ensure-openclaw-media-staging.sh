#!/usr/bin/env bash
# Repair OpenClaw media staging permissions for Signal attachment delivery.

set -euo pipefail

OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"

fail() {
    echo "ERROR: $1" >&2
    exit 1
}

case "$OPENCLAW_HOME" in
    "")
        fail "OPENCLAW_HOME is empty"
        ;;
    /*)
        ;;
    *)
        fail "OPENCLAW_HOME must be an absolute path"
        ;;
esac

case "$OPENCLAW_HOME" in
    /|"$HOME")
        fail "OPENCLAW_HOME points at an unsafe broad directory"
        ;;
    */.openclaw)
        ;;
    *)
        fail "OPENCLAW_HOME must point to a .openclaw directory"
        ;;
esac

MEDIA_DIR="$OPENCLAW_HOME/media"
OUTBOUND_DIR="$MEDIA_DIR/outbound"

mkdir -p "$OUTBOUND_DIR"

find "$OPENCLAW_HOME" -mindepth 1 -maxdepth 1 \
    ! -name media \( -type f -o -type d \) \
    -exec chmod go-rwx {} +

if [ -f "$OPENCLAW_HOME/openclaw.json" ]; then
    chmod 600 "$OPENCLAW_HOME/openclaw.json"
fi

chmod 711 "$OPENCLAW_HOME" "$MEDIA_DIR"
chmod 755 "$OUTBOUND_DIR"

for dir in "$OPENCLAW_HOME" "$MEDIA_DIR" "$OUTBOUND_DIR"; do
    [ -d "$dir" ] || fail "missing directory: $dir"
    [ -x "$dir" ] || fail "directory is not traversable: $dir"
done
