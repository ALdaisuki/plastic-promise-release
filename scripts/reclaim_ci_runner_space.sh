#!/usr/bin/env bash
# Reclaim only disposable GitHub-hosted runner toolchains before heavyweight
# OCI builds. This script must never be used on a developer or production host.
set -euo pipefail

readonly disposable_paths=(
  /usr/local/lib/android
  /usr/share/dotnet
  /opt/ghc
  /opt/hostedtoolcache/CodeQL
)

for path in "${disposable_paths[@]}"; do
  if [[ -e "$path" ]]; then
    sudo rm -rf -- "$path"
  fi
done

df -h /
