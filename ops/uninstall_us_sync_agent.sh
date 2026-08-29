#!/bin/zsh
# Remove the daily us-sync launchd agent installed by install_us_sync_agent.sh.
set -euo pipefail
LABEL="com.cheology.investment-tool.us-sync"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$DEST"
echo "removed: $DEST"
