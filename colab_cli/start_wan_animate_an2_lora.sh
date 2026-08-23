#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export COLAB_WORKFLOW_CONFIG_PRESET="$SCRIPT_DIR/workflows/inso_wan2_2_14b_an2_lora.json"
export COLAB_SESSION_DEFAULT="comfyui-wan-animate-lora"
export COLAB_ACCELERATOR_DEFAULT="3"

exec "$SCRIPT_DIR/start.sh"
