#!/bin/bash
# Post-create setup for devcontainer.
# Called via postCreateCommand in devcontainer.json.
set -e

# Credentials (gh + Claude) are refreshed on every start via postStartCommand.
# Run it here too so first creation also gets credentials.
bash "$(dirname "$0")/refresh-credentials.sh"
