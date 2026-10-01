#!/bin/bash
# Registers the launchd agent "com.jobhuntingbuddy.poll" to run one poll cycle
# every 15 minutes. macOS equivalent of register_task.ps1.
# Run:     ./register_launchd.sh
# Remove:  launchctl bootout gui/$(id -u)/com.jobhuntingbuddy.poll && rm ~/Library/LaunchAgents/com.jobhuntingbuddy.poll.plist
set -euo pipefail

LABEL="com.jobhuntingbuddy.poll"
ROOT="$(cd "$(dirname "$0")" && pwd)"
CMD="$ROOT/run_poll.sh"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

[ -x "$CMD" ] || { echo "run_poll.sh not found or not executable at $CMD" >&2; exit 1; }
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data"

# StartInterval fires every 900s; a run missed during sleep fires on wake.
# launchd never starts a second copy while one is running (like IgnoreNew).
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$CMD</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT</string>
    <key>StartInterval</key>
    <integer>900</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$ROOT/data/launchd.log</string>
    <key>StandardErrorPath</key>
    <string>$ROOT/data/launchd.log</string>
</dict>
</plist>
PLIST

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"

echo "Registered '$LABEL' - runs every 15 minutes."
echo "  Status : launchctl print $DOMAIN/$LABEL | grep -E 'state|last exit'"
echo "  Run now: launchctl kickstart $DOMAIN/$LABEL"
echo "  Log    : $ROOT/data/poll.log"
echo "  Remove : launchctl bootout $DOMAIN/$LABEL && rm $PLIST"
