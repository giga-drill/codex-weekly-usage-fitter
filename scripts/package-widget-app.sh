#!/bin/zsh
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
app_dir="$repo_dir/build/Codex Usage.app"
contents_dir="$app_dir/Contents"
macos_dir="$contents_dir/MacOS"

"$repo_dir/scripts/build-widget.sh" >/dev/null

rm -rf "$app_dir"
mkdir -p "$macos_dir"
cp "$repo_dir/build/codex-usage-widget" "$macos_dir/Codex Usage"
cp "$repo_dir/macos/Info.plist" "$contents_dir/Info.plist"

echo "$app_dir"
