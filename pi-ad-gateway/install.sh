#!/usr/bin/env bash
# Installs the Pi ad-blocking gateway on Raspberry Pi OS Lite (Bookworm or newer).
# Usage: sudo ./install.sh
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }

HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/config/gateway.conf"

if [ "${#AP_PASSPHRASE}" -lt 8 ] || [ "${#AP_PASSPHRASE}" -gt 63 ]; then
  echo "AP_PASSPHRASE must be 8-63 characters." >&2; exit 1
fi
[ "$AP_PASSPHRASE" != "change-me-please" ] || { echo "Set AP_PASSPHRASE in config/gateway.conf first." >&2; exit 1; }

CONF=/etc/pi-gateway
PREFIX=24
DOH_IPS="1.1.1.1, 1.0.0.1, 8.8.8.8, 8.8.4.4, 9.9.9.9, 149.112.112.112, 208.67.222.222, 208.67.220.220, 94.140.14.14, 94.140.15.15"

echo "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq hostapd dnsmasq nftables curl rfkill iw dnsutils

echo "==> Writing configuration to $CONF"
mkdir -p "$CONF"
install -m 600 "$HERE/config/gateway.conf" "$CONF/gateway.conf"
touch "$CONF/custom-blocklist.txt" "$CONF/allowlist.txt"

# Let the Pi keep managing the uplink, but leave the AP interface alone.
if systemctl list-unit-files | grep -q '^NetworkManager.service'; then
  mkdir -p /etc/NetworkManager/conf.d
  cat > /etc/NetworkManager/conf.d/99-gateway-unmanaged.conf <<EOF
[keyfile]
unmanaged-devices=interface-name:$AP_IF
EOF
  systemctl reload NetworkManager || true
fi
if [ -f /etc/dhcpcd.conf ] && ! grep -q "pi-gateway" /etc/dhcpcd.conf; then
  printf '\n# pi-gateway\ndenyinterfaces %s\n' "$AP_IF" >> /etc/dhcpcd.conf
fi

cat > /etc/hostapd/hostapd.conf <<EOF
interface=$AP_IF
driver=nl80211
ssid=$AP_SSID
country_code=$COUNTRY_CODE
ieee80211d=1
hw_mode=g
channel=$AP_CHANNEL
ieee80211n=1
wmm_enabled=1
auth_algs=1
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=$AP_PASSPHRASE
EOF
chmod 600 /etc/hostapd/hostapd.conf
sed -i 's|^#\?DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd

cat > /etc/dnsmasq.conf <<EOF
# Managed by pi-ad-gateway
interface=$AP_IF
bind-interfaces
listen-address=$GATEWAY_IP
domain-needed
bogus-priv
no-resolv
server=$UPSTREAM_DNS_1
server=$UPSTREAM_DNS_2
cache-size=10000

dhcp-range=$DHCP_START,$DHCP_END,255.255.255.0,$DHCP_LEASE
dhcp-option=option:router,$GATEWAY_IP
dhcp-option=option:dns-server,$GATEWAY_IP

# Tell browsers not to auto-enable their own DoH.
address=/use-application-dns.net/
address=/mask.icloud.com/
address=/mask-h2.icloud.com/

conf-file=$CONF/blocklist.conf
EOF
[ -f "$CONF/blocklist.conf" ] || : > "$CONF/blocklist.conf"

cat > /etc/nftables.conf <<EOF
#!/usr/sbin/nft -f
flush ruleset

# NAT + force every client's DNS lookup through the Pi's filter.
table ip gw_nat {
  chain prerouting {
    type nat hook prerouting priority dstnat; policy accept;
    iifname "$AP_IF" ip daddr != $GATEWAY_IP udp dport 53 redirect to :53
    iifname "$AP_IF" ip daddr != $GATEWAY_IP tcp dport 53 redirect to :53
  }
  chain postrouting {
    type nat hook postrouting priority srcnat; policy accept;
    oifname "$UPLINK_IF" masquerade
  }
}

# Stop clients bypassing DNS filtering with encrypted DNS.
table inet gw_filter {
  set doh_servers {
    type ipv4_addr
    elements = { $DOH_IPS }
  }
  chain forward {
    type filter hook forward priority filter; policy accept;
$( [ "$BLOCK_DOT" = "yes" ] && echo "    iifname \"$AP_IF\" tcp dport 853 reject with tcp reset
    iifname \"$AP_IF\" udp dport 853 drop" )
$( [ "$BLOCK_DOH" = "yes" ] && echo "    iifname \"$AP_IF\" ip daddr @doh_servers tcp dport 443 reject with tcp reset
    iifname \"$AP_IF\" ip daddr @doh_servers udp dport 443 drop" )
    # Clients only ever talk to the internet, never to the router's LAN.
    iifname "$AP_IF" oifname "$AP_IF" drop
  }
}
EOF

echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/90-pi-gateway.conf
sysctl -q -p /etc/sysctl.d/90-pi-gateway.conf

echo "==> Installing services"
install -m 755 "$HERE/scripts/update-blocklist.sh" /usr/local/sbin/pi-gateway-update-blocklist
cat > /etc/systemd/system/gateway-ap-ip.service <<EOF
[Unit]
Description=Static address for the access point interface
Before=hostapd.service dnsmasq.service
After=sys-subsystem-net-devices-$AP_IF.device
Wants=sys-subsystem-net-devices-$AP_IF.device

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=-/usr/sbin/rfkill unblock wlan
ExecStartPre=-/usr/bin/iw reg set $COUNTRY_CODE
ExecStart=/sbin/ip addr replace $GATEWAY_IP/$PREFIX dev $AP_IF
ExecStart=/sbin/ip link set $AP_IF up
ExecStop=/sbin/ip addr flush dev $AP_IF

[Install]
WantedBy=multi-user.target
EOF
install -m 644 "$HERE/systemd/gateway-blocklist.service" /etc/systemd/system/
install -m 644 "$HERE/systemd/gateway-blocklist.timer" /etc/systemd/system/

# dnsmasq must start after the AP interface has its address.
mkdir -p /etc/systemd/system/dnsmasq.service.d
cat > /etc/systemd/system/dnsmasq.service.d/gateway.conf <<EOF
[Unit]
After=gateway-ap-ip.service hostapd.service
Requires=gateway-ap-ip.service
EOF

systemctl daemon-reload
systemctl unmask hostapd
systemctl enable gateway-ap-ip hostapd dnsmasq nftables gateway-blocklist.timer >/dev/null

echo "==> Downloading blocklists (needs internet on $UPLINK_IF)"
nft -f /etc/nftables.conf
/usr/local/sbin/pi-gateway-update-blocklist || echo "Blocklist download failed; the timer will retry."

systemctl restart gateway-ap-ip hostapd dnsmasq nftables
systemctl start gateway-blocklist.timer

cat <<EOF

Done.
  Wifi network : $AP_SSID
  Gateway      : $GATEWAY_IP (clients get $DHCP_START-$DHCP_END)
  Uplink       : $UPLINK_IF -> your router

Connect $UPLINK_IF to the router, join "$AP_SSID" from a phone/laptop, and test with:
  ./scripts/test.sh
EOF
