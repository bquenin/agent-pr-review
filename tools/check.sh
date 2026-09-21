#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/extension.test.cjs tests/background.test.cjs
for script in runtime/agent-pr-review runtime/pr-monitor.sh runtime/install.sh vm/install.sh host/install.sh tools/check.sh; do
    bash -n "$script"
done
shellcheck -S warning runtime/agent-pr-review runtime/pr-monitor.sh runtime/install.sh vm/install.sh host/install.sh tools/check.sh
if [ "$(uname -s)" = Darwin ]; then
    swiftc -typecheck host/ClaudeReviewHandler.swift
fi
