#!/usr/bin/env bash
# Install the PiAgent companion client on Linux as a systemd service that runs
# as the unprivileged system user "piagent-client".
#
#   sudo ./install-linux.sh laptop.json     # file from `piagent devices add laptop -o laptop.json`
#   sudo ./install-linux.sh                 # update the script, keep the existing config
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
command -v python3 > /dev/null || { echo "python3 is required" >&2; exit 1; }
command -v systemctl > /dev/null || { echo "systemd is required (or run piagent_client.py run yourself)" >&2; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENROLL="${1:-}"
CONF_DIR=/etc/piagent-client
CONF="$CONF_DIR/config.json"

id -u piagent-client > /dev/null 2>&1 ||
	useradd --system --create-home --home-dir /var/lib/piagent-client --shell /usr/sbin/nologin piagent-client

install -d -m 0755 /opt/piagent-client
install -m 0755 "$HERE/../piagent_client.py" /opt/piagent-client/piagent_client.py
install -d -m 0750 -o root -g piagent-client "$CONF_DIR"

if [ ! -f "$CONF" ]; then
	[ -n "$ENROLL" ] || { echo "first install: sudo $0 ENROLLED.json (from 'piagent devices add NAME -o NAME.json' on the Pi)" >&2; exit 1; }
	python3 /opt/piagent-client/piagent_client.py --config "$CONF" init --from-json "$ENROLL"
fi
chown root:piagent-client "$CONF"
chmod 0640 "$CONF"

install -m 0644 "$HERE/piagent-client.service" /etc/systemd/system/piagent-client.service
systemctl daemon-reload
systemctl enable piagent-client.service
systemctl restart piagent-client.service

echo
echo "Installed. Review the allowlist in $CONF (then: sudo systemctl restart piagent-client)."
echo "Check:  sudo -u piagent-client python3 /opt/piagent-client/piagent_client.py --config $CONF test"
echo "Logs:   journalctl -u piagent-client -f"
