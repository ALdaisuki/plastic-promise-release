#!/usr/bin/env bash
# Thin POSIX launcher for the cross-platform Python deployment controller.
set -euo pipefail

python_bin="${PP_PYTHON:-python3}"
exec "$python_bin" -m plastic_promise.deployment "$@"
