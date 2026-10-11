#!/bin/bash
# Install the PiAgent companion client on macOS as a per-user LaunchAgent.
# It runs as you (not root) whenever you are logged in.
#
#   ./install-macos.sh laptop.json     # file from `piagent devices add laptop -o laptop.json`
#   ./install-macos.sh                 # update the script, keep the existing config
set -euo pipefail

[ "$(id -u)" != 0 ] || { echo "run as your normal user, not with sudo" >&2; exit 1; }
PY="$(command -v python3)" || { echo "python3 is required (xcode-select --install, or python.org)" >&2; exit 1; }

HERE="$(cd "$(dirname "$0")" && pwd)"
ENROLL="${1:-}"
DIR="$HOME/Library/Application Support/piagent-client"
CONF="$DIR/config.json"
PLIST="$HOME/Library/LaunchAgents/com.piagent.client.plist"
LOG="$HOME/Library/Logs/piagent-client.log"

mkdir -p "$DIR" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cp "$HERE/../piagent_client.py" "$DIR/piagent_client.py"
if [ ! -f "$CONF" ]; then
	[ -n "$ENROLL" ] || { echo "first install: $0 ENROLLED.json (from 'piagent devices add NAME -o NAME.json' on the Pi)" >&2; exit 1; }
	"$PY" "$DIR/piagent_client.py" --config "$CONF" init --from-json "$ENROLL"
fi
chmod 600 "$CONF"

cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key><string>com.piagent.client</string>
	<key>ProgramArguments</key>
	<array>
		<string>$PY</string>
		<string>$DIR/piagent_client.py</string>
		<string>--config</string>
		<string>$CONF</string>
		<string>--log</string>
		<string>$LOG</string>
		<string>run</string>
	</array>
	<key>RunAtLoad</key><true/>
	<key>KeepAlive</key><true/>
	<key>ThrottleInterval</key><integer>10</integer>
	<key>ProcessType</key><string>Background</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST" 2> /dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo
echo "Installed. Review the allowlist in: $CONF"
echo "Check:  \"$PY\" \"$DIR/piagent_client.py\" --config \"$CONF\" test"
echo "Logs:   tail -f \"$LOG\""
echo "Remove: launchctl bootout gui/\$(id -u) \"$PLIST\" && rm \"$PLIST\""
