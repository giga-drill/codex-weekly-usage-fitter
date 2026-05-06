#!/bin/zsh
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
binary="$repo_dir/build/codex-usage-widget"

if [[ ! -x "$binary" || "$repo_dir/macos/CodexUsageWidget.swift" -nt "$binary" ]]; then
  "$repo_dir/scripts/build-widget.sh" >/dev/null
fi

exec "$binary" "${1:-$HOME/.codex/usage-monitor/usage.sqlite}"
