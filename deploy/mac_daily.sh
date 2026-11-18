#!/usr/bin/env bash
# Run JobMan every day at 08:30 on a Mac (launchd). If the Mac is asleep then, it runs when it wakes.
#   deploy/mac_daily.sh          switch the daily run on
#   deploy/mac_daily.sh off      switch it off
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.jobman.daily.plist"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
if [ "${1:-}" = "off" ]; then rm -f "$PLIST"; echo "Daily run switched off."; exit 0; fi
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.jobman.daily</string>
  <key>ProgramArguments</key><array>
    <string>/bin/zsh</string><string>-ic</string>
    <string>cd "$DIR" &amp;&amp; echo "=== \$(date) ===" &gt;&gt; run.log &amp;&amp; .venv/bin/python -m jobpilot run &gt;&gt; run.log 2&gt;&amp;1</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>30</integer></dict>
</dict></plist>
PL
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Daily run switched on (08:30). Log: $DIR/run.log"
