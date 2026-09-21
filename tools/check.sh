#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/extension.test.cjs tests/background.test.cjs
for script in vm/agent-pr-review vm/pr-monitor.sh vm/install.sh host/install.sh tools/check.sh; do
    bash -n "$script"
done
shellcheck -S warning vm/agent-pr-review vm/pr-monitor.sh vm/install.sh host/install.sh tools/check.sh
if [ "$(uname -s)" = Darwin ]; then
    swiftc -typecheck host/ClaudeReviewHandler.swift
fi
