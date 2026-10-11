#!/bin/bash -e
# Headless, CLI-only system with as few services as practical.

# Services a headless LLM appliance does not need. Missing units are skipped.
on_chroot << 'EOF'
for unit in udisks2.service ModemManager.service triggerhappy.service triggerhappy.socket \
	dphys-swapfile.service cups.service cups-browsed.service rpcbind.service rpcbind.socket \
	nfs-common.service apt-daily.timer apt-daily-upgrade.timer man-db.timer e2scrub_all.timer \
	rsync.service; do
	if systemctl cat "$unit" > /dev/null 2>&1; then
		systemctl disable "$unit" > /dev/null 2>&1 || true
		echo "disabled $unit"
	fi
done
systemctl set-default multi-user.target
EOF

if [ "${PIAGENT_DISABLE_BT:-1}" = "1" ]; then
	on_chroot << 'EOF'
for unit in bluetooth.service hciuart.service; do
	if systemctl cat "$unit" > /dev/null 2>&1; then
		systemctl mask "$unit"
	fi
done
EOF
	if ! grep -q "^dtoverlay=disable-bt" "${ROOTFS_DIR}/boot/firmware/config.txt"; then
		cat >> "${ROOTFS_DIR}/boot/firmware/config.txt" << 'EOF'

[all]
# PiAgent: headless appliance, Bluetooth off
dtoverlay=disable-bt
EOF
	fi
fi

# Optional Wi-Fi (Ethernet needs no setup). WPA_COUNTRY in the build config
# unblocks the radio; this adds the network itself.
if [ -n "${PIAGENT_WIFI_SSID:-}" ]; then
	conn="${ROOTFS_DIR}/etc/NetworkManager/system-connections/piagent-wifi.nmconnection"
	install -d -m 0700 "$(dirname "$conn")"
	uuid="$(cat /proc/sys/kernel/random/uuid)"
	{
		echo "[connection]"
		echo "id=piagent-wifi"
		echo "uuid=${uuid}"
		echo "type=wifi"
		echo "autoconnect=true"
		echo
		echo "[wifi]"
		echo "mode=infrastructure"
		echo "ssid=${PIAGENT_WIFI_SSID}"
		echo
		if [ -n "${PIAGENT_WIFI_PSK:-}" ]; then
			echo "[wifi-security]"
			echo "key-mgmt=wpa-psk"
			echo "psk=${PIAGENT_WIFI_PSK}"
			echo
		fi
		echo "[ipv4]"
		echo "method=auto"
		echo
		echo "[ipv6]"
		echo "method=auto"
	} > "$conn"
	chmod 0600 "$conn"
	chown 0:0 "$conn"
fi

# Lower the swap appetite; with dphys-swapfile off there is no swap file, so
# the 8 GB of RAM is shared by the model (~2.5 GB) and the KV cache (~0.5 GB).
echo "vm.swappiness=10" > "${ROOTFS_DIR}/etc/sysctl.d/90-piagent.conf"
