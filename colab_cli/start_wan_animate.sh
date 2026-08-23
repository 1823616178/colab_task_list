#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export COLAB_WORKFLOW_CONFIG_PRESET="$SCRIPT_DIR/workflows/inso_wan2_2_14.json"
export COLAB_SESSION_DEFAULT="comfyui-wan-animate"

exec "$SCRIPT_DIR/start.sh"
