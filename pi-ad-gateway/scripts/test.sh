#!/usr/bin/env bash
# Run on the Pi (or a connected client) to check the gateway is working.
source /etc/pi-gateway/gateway.conf 2>/dev/null || source "$(dirname "$0")/../config/gateway.conf"

ok()   { echo "  PASS  $1"; }
bad()  { echo "  FAIL  $1"; }

for svc in gateway-ap-ip hostapd dnsmasq nftables; do
  systemctl is-active --quiet "$svc" && ok "$svc running" || bad "$svc not running"
done

[ "$(sysctl -n net.ipv4.ip_forward)" = "1" ] && ok "IP forwarding on" || bad "IP forwarding off"

r=$(dig +short @"$GATEWAY_IP" doubleclick.net 2>/dev/null | head -1)
[ "$r" = "0.0.0.0" ] && ok "doubleclick.net blocked" || bad "doubleclick.net not blocked (got: ${r:-nothing})"

r=$(dig +short @"$GATEWAY_IP" example.com 2>/dev/null | head -1)
[ -n "$r" ] && [ "$r" != "0.0.0.0" ] && ok "example.com resolves ($r)" || bad "example.com did not resolve"

echo "blocklist size: $(wc -l < /etc/pi-gateway/blocklist.conf 2>/dev/null || echo 0) domains"
