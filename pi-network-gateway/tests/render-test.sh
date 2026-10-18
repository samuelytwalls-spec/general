#!/bin/bash
# shellcheck disable=SC2016  # literal $ in test passwords is intentional
# Offline test: renders every config for several setups and checks the output.
# Runs on any Linux box (no Pi, no Wi-Fi needed). If 'nft' is available and we
# are root, the generated ruleset is also syntax-checked with `nft -c`, and the
# dnsmasq config with `dnsmasq --test` when dnsmasq is installed.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

export PIGW_LIB=$ROOT/lib PIGW_RUN=$WORK/run PIGW_STATE=$WORK/state
# shellcheck source=../lib/common.sh
source "$ROOT/lib/common.sh"
# shellcheck source=../lib/render.sh
source "$ROOT/lib/render.sh"

fails=0
check() {  # check FILE PATTERN [absent]
    if [[ ${3:-} == absent ]]; then
        if grep -qE -- "$2" "$1"; then echo "  FAIL: '$2' should not be in $(basename "$1")"; fails=$((fails+1)); fi
    elif ! grep -qE -- "$2" "$1"; then
        echo "  FAIL: '$2' missing from $(basename "$1")"; fails=$((fails+1))
    fi
}

run_case() {  # run_case NAME "KEY=VALUE ..."
    local name=$1; shift
    echo "case: $name"
    PIGW_CONF=$WORK/$name.conf
    cp "$ROOT/config/gateway.conf.example" "$PIGW_CONF"
    set_conf_value AP_PASSWORD 'p@ss "word"; $x'
    local kv
    for kv in "$@"; do set_conf_value "${kv%%=*}" "${kv#*=}"; done
    load_config
    validate_config
    mkdir -p "$WORK/$name"
    render_hostapd "$WORK/$name/hostapd.conf"
    render_dnsmasq "$WORK/$name/dnsmasq.conf"
    render_nft "$WORK/$name/rules.nft"
    if command -v nft >/dev/null && ((EUID == 0)); then
        nft -c -f "$WORK/$name/rules.nft" || { echo "  FAIL: nft rejected rules.nft"; fails=$((fails+1)); }
    fi
    if command -v dnsmasq >/dev/null; then
        dnsmasq --test --conf-file="$WORK/$name/dnsmasq.conf" 2>/dev/null \
            || { echo "  FAIL: dnsmasq rejected dnsmasq.conf"; fails=$((fails+1)); }
    fi
}

run_case eth-wpa2
d=$WORK/eth-wpa2
check "$d/hostapd.conf" '^interface=wlan0$'
check "$d/hostapd.conf" '^ignore_broadcast_ssid=1$'
check "$d/hostapd.conf" '^wpa_passphrase=p@ss "word"; \$x$'
check "$d/hostapd.conf" '^wpa_key_mgmt=WPA-PSK$'
check "$d/hostapd.conf" '^hw_mode=g$'
check "$d/hostapd.conf" '^channel=6$'
check "$d/dnsmasq.conf" '^dhcp-range=192.168.50.10,192.168.50.200,255.255.255.0,12h$'
check "$d/dnsmasq.conf" '^server=1.1.1.1$'
check "$d/dnsmasq.conf" '^port=0$' absent
check "$d/rules.nft" 'oifname "eth0" ip saddr 192.168.50.0/24 masquerade'
check "$d/rules.nft" 'tcp dport \{ 22 \} accept'
check "$d/rules.nft" '192.168.0.0/16' absent

run_case pihole-5ghz-wpa3 PIHOLE=yes AP_BAND=5 AP_CHANNEL=36 AP_SECURITY=wpa2-wpa3 \
    AP_HIDDEN=no BLOCK_UPSTREAM_LAN=yes ALLOW_SSH_FROM_AP=no LAN_ADDRESS=10.50.0.1 \
    LAN_PREFIX=24 DHCP_START=10.50.0.10 DHCP_END=10.50.0.99
d=$WORK/pihole-5ghz-wpa3
check "$d/hostapd.conf" '^hw_mode=a$'
check "$d/hostapd.conf" '^vht_oper_centr_freq_seg0_idx=42$'
check "$d/hostapd.conf" '^ht_capab=\[HT40\+\]'
check "$d/hostapd.conf" '^wpa_key_mgmt=WPA-PSK SAE$'
check "$d/hostapd.conf" '^ieee80211w=1$'
check "$d/hostapd.conf" '^ignore_broadcast_ssid=0$'
check "$d/dnsmasq.conf" '^port=0$'
check "$d/dnsmasq.conf" '^dhcp-option=option:dns-server,10.50.0.1$'
check "$d/dnsmasq.conf" '^server=' absent
check "$d/rules.nft" 'ip saddr 10.50.0.0/24 masquerade'
check "$d/rules.nft" 'tcp dport \{ 80,443 \} accept'
check "$d/rules.nft" '192.168.0.0/16'

run_case single-radio UPSTREAM_IFACE=wlan0 AP_IFACE=uap0 AP_VIRTUAL=yes
d=$WORK/single-radio
RADIO_BAND=5 RADIO_CHANNEL=44 RADIO_NARROW=yes render_hostapd "$d/hostapd.conf"
check "$d/hostapd.conf" '^interface=uap0$'
check "$d/hostapd.conf" '^channel=44$'
check "$d/hostapd.conf" '^vht_oper_chwidth=0$'
check "$d/rules.nft" 'oifname "wlan0" ip saddr'

echo "case: USB link (no Wi-Fi settings needed)"
PIGW_CONF=$WORK/usb.conf
cp "$ROOT/config/gateway.conf.example" "$PIGW_CONF"
set_conf_value CLIENT_LINK usb
set_conf_value UPSTREAM_IFACE wlan0
load_config; validate_config     # example password must not matter here
[[ $AP_IFACE == pigw0 ]] || { echo "  FAIL: USB mode should use pigw0, got $AP_IFACE"; fails=$((fails+1)); }
mkdir -p "$WORK/usb"
render_dnsmasq "$WORK/usb/dnsmasq.conf"
render_nft "$WORK/usb/rules.nft"
check "$WORK/usb/dnsmasq.conf" '^interface=pigw0$'
check "$WORK/usb/rules.nft" 'iifname "pigw0" jump ap_to_pi'
check "$WORK/usb/rules.nft" 'oifname "wlan0" ip saddr 192.168.50.0/24 masquerade'
if command -v nft >/dev/null && ((EUID == 0)); then
    nft -c -f "$WORK/usb/rules.nft" || { echo "  FAIL: nft rejected USB rules"; fails=$((fails+1)); }
fi

echo "case: Ethernet link"
PIGW_CONF=$WORK/eth.conf
cp "$ROOT/config/gateway.conf.example" "$PIGW_CONF"
set_conf_value CLIENT_LINK ethernet
set_conf_value UPSTREAM_IFACE wlan0
set_conf_value AP_IFACE eth0
load_config; validate_config
render_nft "$WORK/eth-rules.nft"
check "$WORK/eth-rules.nft" 'oifname "wlan0" ip saddr'

echo "case: static leases + system DNS"
PIGW_CONF=$WORK/static.conf
cp "$ROOT/config/gateway.conf.example" "$PIGW_CONF"
sed -i -e 's/^AP_PASSWORD=.*/AP_PASSWORD=supersecret/' -e 's/^UPSTREAM_DNS=.*/UPSTREAM_DNS=""/' \
    -e 's|^  # "aa:bb:cc:dd:ee:ff,192.168.50.20,game-console"|  "aa:bb:cc:dd:ee:ff,192.168.50.20,game-console"|' "$PIGW_CONF"
load_config; validate_config
render_dnsmasq "$WORK/static-dnsmasq.conf"
check "$WORK/static-dnsmasq.conf" '^dhcp-host=aa:bb:cc:dd:ee:ff,192.168.50.20,game-console$'
check "$WORK/static-dnsmasq.conf" '^resolv-file=/etc/resolv.conf$'

echo "case: invalid configs are rejected"
bad() {
    PIGW_CONF=$WORK/bad.conf
    cp "$ROOT/config/gateway.conf.example" "$PIGW_CONF"
    set_conf_value AP_PASSWORD supersecret
    set_conf_value "${1%%=*}" "${1#*=}"
    if (load_config; validate_config) 2>/dev/null; then
        echo "  FAIL: accepted $1"; fails=$((fails+1))
    fi
}
bad AP_PASSWORD=short
bad AP_PASSWORD=change-this-password
bad AP_SSID=this-ssid-is-way-too-long-for-802.11
bad AP_CHANNEL=52
bad AP_BAND=6
bad DHCP_START=10.0.0.5
bad LAN_ADDRESS=300.1.1.1
bad UPSTREAM_IFACE=wlan0
bad AP_COUNTRY=us
bad 'UPSTREAM_DNS=1.1.1.1 nope'
bad CLIENT_LINK=bluetooth

echo "case: freq2chan"
[[ $(freq2chan 2437) == 6 && $(freq2chan 2484) == 14 && $(freq2chan 5180) == 36 && $(freq2chan 5745) == 149 ]] \
    || { echo "  FAIL: freq2chan"; fails=$((fails+1)); }

echo "case: subnets_overlap"
subnets_overlap 192.168.50.1/24 192.168.50.38/24 || { echo "  FAIL: same /24 not detected"; fails=$((fails+1)); }
subnets_overlap 192.168.50.1/24 192.168.0.5/16   || { echo "  FAIL: /16 containing /24 not detected"; fails=$((fails+1)); }
! subnets_overlap 10.50.0.1/24 192.168.50.38/24  || { echo "  FAIL: disjoint subnets reported as overlapping"; fails=$((fails+1)); }

if ((fails)); then echo "$fails check(s) failed"; exit 1; fi
echo "all checks passed"
