#!/usr/bin/env bash
# Thin POSIX launcher for the cross-platform Python deployment controller.
set -euo pipefail

python_bin="${PP_PYTHON:-python3}"
script_dir="$(cd "$(dirname "$0")" && pwd)"

if [ "${1:-}" = "onboard" ]; then
  shift
  # One-time trust bootstrap + permanent tunnel service.
  exec "$python_bin" "$script_dir/compute_node_handshake.py" --mode onboard "$@"
fi

if [ "${1:-}" = "handshake" ]; then
  shift
  # One-click compute-node private-transport handshake.
  exec "$python_bin" "$script_dir/compute_node_handshake.py" "$@"
fi

exec "$python_bin" -m plastic_promise.deployment "$@"
