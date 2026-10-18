#!/bin/bash
# Removes Pi Gateway. Keeps /etc/pi-gateway unless --purge is given.
# Pi-hole, if installed, is left alone: remove it with 'pihole uninstall'.
set -uo pipefail

((EUID == 0)) || { echo "run as root: sudo $0" >&2; exit 1; }

systemctl disable --now pi-gateway-watch.timer 2>/dev/null
systemctl disable --now pi-gateway.target 2>/dev/null
systemctl stop pi-gateway-dhcp.service pi-gateway-hostapd.service pi-gateway-net.service 2>/dev/null
rm -f /etc/systemd/system/pi-gateway.target /etc/systemd/system/pi-gateway-*.service \
      /etc/systemd/system/pi-gateway-*.timer
systemctl daemon-reload

rm -f /usr/local/bin/pi-gateway
rm -rf /usr/local/lib/pi-gateway /run/pi-gateway

if [[ -f /etc/NetworkManager/conf.d/99-pi-gateway.conf ]]; then
    rm -f /etc/NetworkManager/conf.d/99-pi-gateway.conf
    systemctl reload NetworkManager 2>/dev/null
fi
for bootcfg in /boot/firmware/config.txt /boot/config.txt; do
    [[ -f $bootcfg ]] && sed -i '/^# pi-gateway: USB-C port acts as a USB network adapter$/,+2d' "$bootcfg"
done
if [[ -f /etc/dhcpcd.conf ]]; then
    sed -i '/^# pi-gateway$/,+1d' /etc/dhcpcd.conf
fi

if [[ ${1:-} == --purge ]]; then
    rm -rf /etc/pi-gateway /var/lib/pi-gateway
    echo "Pi Gateway removed, including its configuration."
else
    echo "Pi Gateway removed. Config kept in /etc/pi-gateway (use --purge to delete)."
fi
command -v pihole >/dev/null && echo "Pi-hole is still installed; run 'pihole uninstall' to remove it."
exit 0
