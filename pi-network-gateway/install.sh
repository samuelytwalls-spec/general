#!/bin/bash
# Pi Gateway installer for Raspberry Pi OS (Bookworm/Trixie) on a Raspberry Pi 5.
#
#   sudo ./install.sh                     interactive setup
#   sudo ./install.sh --config my.conf    use a prepared gateway.conf
#   sudo ./install.sh --reinstall         reinstall files, keep existing config
set -euo pipefail

SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export PIGW_LIB=$SRC/lib
# shellcheck source=lib/common.sh
source "$SRC/lib/common.sh"

CONFIG_SRC=""
REINSTALL=no
while (($#)); do
    case $1 in
        --config) CONFIG_SRC=${2:?--config needs a file}; shift 2 ;;
        --reinstall) REINSTALL=yes; shift ;;
        -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

((EUID == 0)) || die "run as root: sudo $0"
grep -qiE 'debian|raspbian' /etc/os-release || die "needs Raspberry Pi OS / Debian"
model=$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo unknown)
[[ $model == *"Raspberry Pi 5"* ]] || warn "this is '$model', not a Raspberry Pi 5 - continuing anyway"

ask() {  # ask VAR "question" default
    local __var=$1 __q=$2 __def=${3:-} __ans
    read -r -p "$__q${__def:+ [$__def]}: " __ans
    printf -v "$__var" '%s' "${__ans:-$__def}"
}
ask_yn() {  # ask_yn "question" y|n  -> exit status
    local a
    read -r -p "$1 [$( [[ $2 == y ]] && echo Y/n || echo y/N )]: " a
    a=${a:-$2}
    [[ ${a,,} == y* ]]
}
random_password() { LC_ALL=C tr -dc 'A-HJ-NP-Za-km-z2-9' </dev/urandom | head -c 16 || true; }
iface_ip() { ip -4 -o addr show dev "$1" 2>/dev/null | awk '{ split($4, a, "/"); print a[1]; exit }'; }
wifi_connected() { iw dev "$1" link 2>/dev/null | grep -q '^Connected'; }

# --- Packages ----------------------------------------------------------------
log "installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq hostapd dnsmasq-base iw nftables rfkill qrencode curl wireless-regdb >/dev/null

# --- Configuration -----------------------------------------------------------
wizard() {
    local conf=$1 mode ssid pw pw2 band country rssid rpw
    cp "$SRC/config/gateway.conf.example" "$conf"
    chmod 600 "$conf"
    PIGW_CONF=$conf

    echo
    echo "=== Pi Gateway setup ==="
    echo "How does the Pi reach your router, and which radio runs the hidden network?"
    echo "  1) Ethernet cable to the router, built-in Wi-Fi is the hidden AP   (recommended)"
    echo "  2) Built-in Wi-Fi to the router, USB Wi-Fi adapter is the hidden AP"
    echo "  3) Built-in Wi-Fi does both (single radio, AP follows router's channel)"
    local def=1
    [[ $(cat /sys/class/net/eth0/carrier 2>/dev/null) == 1 ]] || def=3
    [[ $def == 3 && -d /sys/class/net/wlan1 ]] && def=2
    ask mode "Choice" "$def"
    case $mode in
        1) set_conf_value UPSTREAM_IFACE eth0;  set_conf_value AP_IFACE wlan0; set_conf_value AP_VIRTUAL no ;;
        2) set_conf_value UPSTREAM_IFACE wlan0; set_conf_value AP_IFACE wlan1; set_conf_value AP_VIRTUAL no ;;
        3) set_conf_value UPSTREAM_IFACE wlan0; set_conf_value AP_IFACE uap0;  set_conf_value AP_VIRTUAL yes
           set_conf_value AP_PARENT_IFACE wlan0 ;;
        *) die "invalid choice" ;;
    esac

    if [[ $mode == 1 ]] && ip -4 route show default | grep -q 'dev wlan0'; then
        warn "the Pi currently reaches the network over wlan0. Option 1 turns wlan0 into the"
        warn "access point, which will drop that connection (and any SSH session over it)."
        ask_yn "Continue?" n || die "aborted - plug in Ethernet first, or choose option 2/3"
    fi
    if [[ $mode != 1 ]] && ! wifi_connected wlan0; then
        echo "wlan0 is not connected to your router yet."
        ask rssid "Router Wi-Fi name (SSID)" ""
        read -r -s -p "Router Wi-Fi password: " rpw; echo
        nmcli dev wifi connect "$rssid" password "$rpw" ifname wlan0 \
            || die "could not connect wlan0 to '$rssid'"
    fi

    echo
    local suffix
    suffix=$(random_password)
    ask ssid "Hidden network name (SSID)" "PiGateway-${suffix:0:4}"
    set_conf_value AP_SSID "$ssid"
    while :; do
        read -r -s -p "Hidden network password (8-63 chars, empty = generate one): " pw; echo
        if [[ -z $pw ]]; then pw=$(random_password); echo "Generated password: $pw"; break; fi
        (( ${#pw} >= 8 && ${#pw} <= 63 )) || { echo "Must be 8-63 characters."; continue; }
        read -r -s -p "Repeat: " pw2; echo
        [[ $pw == "$pw2" ]] && break
        echo "Passwords do not match."
    done
    set_conf_value AP_PASSWORD "$pw"
    if ask_yn "Hide the network name (not broadcast)?" y; then
        set_conf_value AP_HIDDEN yes
    else
        set_conf_value AP_HIDDEN no
    fi

    if [[ $mode != 3 ]]; then
        ask band "Band: 2.4 (range, compatibility) or 5 (speed)" "2.4"
        case $band in
            5) set_conf_value AP_BAND 5; set_conf_value AP_CHANNEL 36 ;;
            *) set_conf_value AP_BAND 2.4; set_conf_value AP_CHANNEL 6 ;;
        esac
    fi
    country=$(iw reg get 2>/dev/null | awk '/^country/ { sub(":", "", $2); print $2; exit }')
    [[ $country =~ ^[A-Z]{2}$ && $country != 00 ]] || country=US
    ask country "Wi-Fi country code" "$country"
    set_conf_value AP_COUNTRY "${country^^}"

    # Pick a client subnet that does not collide with the router's.
    local up_ip
    up_ip=$(iface_ip "$( [[ $mode == 1 ]] && echo eth0 || echo wlan0 )")
    if [[ $up_ip == 192.168.50.* ]]; then
        set_conf_value LAN_ADDRESS 10.50.0.1
        set_conf_value DHCP_START 10.50.0.10
        set_conf_value DHCP_END 10.50.0.200
    fi

    if ask_yn "Install Pi-hole for ad/tracker blocking on the hidden network?" n; then
        set_conf_value PIHOLE yes
        read -r -s -p "Pi-hole admin password (empty = Pi-hole default): " pw; echo
        set_conf_value PIHOLE_WEB_PASSWORD "$pw"
    fi
}

install -d -m 755 /etc/pi-gateway
if [[ -n $CONFIG_SRC ]]; then
    install -m 600 "$CONFIG_SRC" /etc/pi-gateway/gateway.conf
elif [[ -e /etc/pi-gateway/gateway.conf ]]; then
    if [[ $REINSTALL == no ]] && ask_yn "Existing config found. Run the setup wizard again?" n; then
        wizard /etc/pi-gateway/gateway.conf
    fi
else
    wizard /etc/pi-gateway/gateway.conf
fi
PIGW_CONF=/etc/pi-gateway/gateway.conf
chmod 600 "$PIGW_CONF"
load_config
validate_config

# --- Files -------------------------------------------------------------------
log "installing files"
install -d -m 755 /usr/local/lib/pi-gateway
install -m 644 "$SRC/lib/common.sh" "$SRC/lib/render.sh" /usr/local/lib/pi-gateway/
install -m 755 "$SRC/lib/net-up" "$SRC/lib/net-down" "$SRC/lib/pihole-setup" /usr/local/lib/pi-gateway/
install -m 755 "$SRC/bin/pi-gateway" /usr/local/bin/pi-gateway
install -m 644 "$SRC"/systemd/pi-gateway*.service "$SRC/systemd/pi-gateway.target" /etc/systemd/system/
install -d -m 755 /var/lib/pi-gateway

# --- Wi-Fi regulatory domain -------------------------------------------------
if command -v raspi-config >/dev/null; then
    raspi-config nonint do_wifi_country "$AP_COUNTRY" >/dev/null 2>&1 || warn "could not set Wi-Fi country"
fi
rfkill unblock wlan || true

# --- Keep NetworkManager / dhcpcd away from the AP interface -----------------
if [[ -d /etc/NetworkManager ]]; then
    install -d /etc/NetworkManager/conf.d
    cat >/etc/NetworkManager/conf.d/99-pi-gateway.conf <<EOF
# Managed by pi-gateway: the access point interface belongs to hostapd.
[keyfile]
unmanaged-devices=interface-name:$AP_IFACE
EOF
    systemctl reload NetworkManager 2>/dev/null || true
fi
if [[ -f /etc/dhcpcd.conf ]] && ! grep -q 'pi-gateway' /etc/dhcpcd.conf; then
    printf '\n# pi-gateway\ndenyinterfaces %s\n' "$AP_IFACE" >>/etc/dhcpcd.conf
fi

# --- Services that would fight over the radio or ports 53/67 -----------------
for svc in hostapd.service dnsmasq.service; do
    if systemctl is-enabled "$svc" >/dev/null 2>&1 || systemctl is-active "$svc" >/dev/null 2>&1; then
        warn "disabling stock $svc (pi-gateway runs its own instance)"
        systemctl disable --now "$svc" >/dev/null 2>&1 || true
    fi
done

# --- Pi-hole -----------------------------------------------------------------
if is_yes "$PIHOLE"; then
    systemctl stop pi-gateway-dhcp.service 2>/dev/null || true
    PIGW_LIB=/usr/local/lib/pi-gateway /usr/local/lib/pi-gateway/pihole-setup enable
fi

# --- Start -------------------------------------------------------------------
systemctl daemon-reload
systemctl enable pi-gateway.target >/dev/null
log "starting the gateway"
systemctl restart pi-gateway.target
sleep 3

echo
/usr/local/bin/pi-gateway status || true
echo
if ! systemctl is-active --quiet pi-gateway-hostapd.service; then
    warn "the access point did not start - see: journalctl -u pi-gateway-hostapd -b"
fi
echo "Scan this on a phone to join the hidden network:"
/usr/local/bin/pi-gateway qr || true
echo
echo "Done. Manage it with:  pi-gateway status | clients | set-password | qr | logs"
