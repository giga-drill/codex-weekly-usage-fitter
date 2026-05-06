#!/bin/zsh
set -u

MODE="${1:-ensure-daemon}"
LABEL="gui/$(id -u)/local.codex-usage"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHONPATH_VALUE="$REPO_DIR/src"
LOG_DIR="$HOME/.codex/usage-monitor"
LOG_FILE="$LOG_DIR/hook.log"

mkdir -p "$LOG_DIR"

log() {
  print -r -- "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*" >> "$LOG_FILE"
}

if [[ ! -d "$PYTHONPATH_VALUE" ]]; then
  log "missing PYTHONPATH directory: $PYTHONPATH_VALUE"
  exit 0
fi

if [[ "$MODE" == "sample-stop" ]]; then
  payload="$(cat)"
  launchctl kickstart "$LABEL" >/dev/null 2>&1 || true
  if ! printf '%s' "$payload" | PYTHONPATH="$PYTHONPATH_VALUE" /usr/bin/python3 -m codex_usage sample-stop 2>> "$LOG_FILE"; then
    log "sample-stop failed"
  fi
else
  launchctl kickstart "$LABEL" >/dev/null 2>&1 || true
fi

exit 0
