#!/bin/bash
# Removes the app and its login item. Recordings (~/Teams Recordings) and the voice database are kept.
set -u
APP="$HOME/Applications/Edison.app"
for pid in $(pgrep -f "$APP/Contents/MacOS/Edison" 2>/dev/null); do kill "$pid" 2>/dev/null; done
sleep 1
OLD="$HOME/Library/LaunchAgents/local.rubenaraujo.edison.plist"
[ -f "$OLD" ] && { launchctl bootout "gui/$(id -u)" "$OLD" 2>/dev/null; rm -f "$OLD"; }
rm -rf "$APP"
tccutil reset ScreenCapture local.rubenaraujo.edison >/dev/null 2>&1
tccutil reset Microphone local.rubenaraujo.edison >/dev/null 2>&1
echo "Edison removed. Your recordings and data/voices.db were kept."
echo "To free disk space, delete this folder and ~/.cache/huggingface (Whisper/Qwen models)."
