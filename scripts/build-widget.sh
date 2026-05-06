#!/bin/zsh
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
out_dir="$repo_dir/build"
mkdir -p "$out_dir"

swiftc \
  "$repo_dir/macos/CodexUsageWidget.swift" \
  -framework AppKit \
  -lsqlite3 \
  -o "$out_dir/codex-usage-widget"

echo "$out_dir/codex-usage-widget"
