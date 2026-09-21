#!/usr/bin/env bash
# Compatibility entry point for installations using the old runtime directory.
set -euo pipefail
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/runtime/install.sh" "$@"
