#!/usr/bin/env bash
# Compatibility wrapper — CI and docs may call build_mac.sh
set -euo pipefail
cd "$(dirname "$0")"
exec ./build_macos.sh "$@"
