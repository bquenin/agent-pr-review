#!/usr/bin/env bash
# Install runtime files from this checkout into the current Linux environment.
# Installed files are copies; rerun after source updates. The Mac installer is
# separate so neither environment overwrites the other's installed runtime.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

bin_dir="$HOME/.local/bin"
resource_dir="${AGENT_PR_REVIEW_RESOURCES:-$HOME/.local/share/agent-pr-review}"

python3 "$repo_root/lib/review_config.py" validate
mkdir -p "$bin_dir" "$resource_dir"
install -m 0644 "$repo_root/lib/review_config.py" "$resource_dir/review_config.py"

install -m 0755 "$repo_root/runtime/agent-pr-review"            "$bin_dir/agent-pr-review"
install -m 0755 "$repo_root/runtime/t3-review.py"                "$resource_dir/t3-review.py"
install -m 0755 "$repo_root/runtime/t3_monitor.py"                "$resource_dir/t3_monitor.py"
install -m 0755 "$repo_root/runtime/pr-monitor.sh"              "$resource_dir/pr-monitor.sh"
install -m 0644 "$repo_root/prompts/review-methodology.md" "$resource_dir/review-methodology.md"
install -m 0644 "$repo_root/prompts/review-posting-rules.md" "$resource_dir/review-posting-rules.md"

echo "Installed:"
echo "  $bin_dir/agent-pr-review"
echo "  $resource_dir/t3-review.py"
echo "  $resource_dir/t3_monitor.py"
echo "  $resource_dir/pr-monitor.sh"
echo "  $resource_dir/review-methodology.md"
echo "  $resource_dir/review-posting-rules.md"

# Cron restarts saved T3 watchers after a host reboot or a worker crash. Hosts
# without cron still get detached watchers on every launch.
if [ "${1:-}" = "--enable-supervision" ] && command -v crontab >/dev/null 2>&1; then
    if ! python3 "$resource_dir/t3_monitor.py" --install-cron; then
        echo "WARNING: T3 watcher supervision could not be installed; reopen a PR after a host restart." >&2
    fi
else
    echo "NOTE: T3 cron supervision is optional: runtime/install.sh --enable-supervision"
fi
python3 "$resource_dir/t3_monitor.py" --ensure

case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) echo "WARNING: $bin_dir is not on PATH; add it in ~/.bashrc" >&2 ;;
esac
