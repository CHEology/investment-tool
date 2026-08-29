#!/bin/zsh
# Install (or reinstall) the daily us-sync launchd agent for the CURRENT user.
# Ingestion-only scheduling (PR-G): the job runs `invest us-sync-daily`, which
# catches up pending SEC filing days, polls halts, and appends the soak
# ledger. It never runs the opportunity trial.
#
# Uninstall with: ops/uninstall_us_sync_agent.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$REPO/.venv/bin/python"
LABEL="com.cheology.investment-tool.us-sync"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ ! -x "$PYTHON" ]]; then
  echo "error: $PYTHON not found — create the venv first (uv venv; uv pip install -e '.[dev]')" >&2
  exit 1
fi

mkdir -p "$REPO/data/audit/soak" "$HOME/Library/LaunchAgents"
sed -e "s|__REPO__|$REPO|g" -e "s|__PYTHON__|$PYTHON|g" \
  "$REPO/ops/launchd/$LABEL.plist.template" > "$DEST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
launchctl print "gui/$(id -u)/$LABEL" | head -5
echo "installed: $DEST (daily 19:30 local; logs in data/audit/soak/)"
