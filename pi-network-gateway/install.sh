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

# Keep a log, and keep going if the SSH session drops (it does when the Wi-Fi
# the session runs over becomes the access point).
LOG=/var/log/pi-gateway-install.log
trap '' HUP
exec > >(tee --output-error=warn -a "$LOG") 2>&1
log "install started $(date -Is), log: $LOG"
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
wifi_connected() { iw dev "$1" link 2>/dev/null | grep -q '^Connected'; }

# --- Packages ----------------------------------------------------------------
log "installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq hostapd dnsmasq-base iw nftables rfkill qrencode curl wireless-regdb >/dev/null

# --- Configuration -----------------------------------------------------------
wizard() {
    local conf=$1 link mode ssid pw pw2 band country rssid rpw up
    # Tear down a running gateway with its current config before replacing it
    # (switching modes leaves interfaces behind otherwise).
    systemctl stop pi-gateway.target pi-gateway-dhcp.service pi-gateway-hostapd.service \
        pi-gateway-net.service 2>/dev/null || true
    cp "$SRC/config/gateway.conf.example" "$conf"
    chmod 600 "$conf"
    PIGW_CONF=$conf

    echo
    echo "=== Pi Gateway setup ==="
    echo "How will the blocked device connect to the Pi?"
    echo "  1) Hidden Wi-Fi network made by the Pi"
    echo "  2) USB cable: computer <-> Pi's USB-C port (the computer also powers the Pi)"
    echo "  3) Ethernet cable: device <-> Pi's Ethernet port"
    ask link "Choice" 1
    case $link in
        1) set_conf_value CLIENT_LINK wifi; wizard_wifi_modes ;;
        2) set_conf_value CLIENT_LINK usb
           set_conf_value AP_IFACE pigw0; set_conf_value AP_VIRTUAL no
           echo "How does the Pi reach your router?"
           echo "  1) Wi-Fi (wlan0)"
           echo "  2) Ethernet cable (eth0)"
           ask up "Choice" 1
           if [[ $up == 2 ]]; then mode=1; set_conf_value UPSTREAM_IFACE eth0
           else mode=2; set_conf_value UPSTREAM_IFACE wlan0
           fi ;;
        3) set_conf_value CLIENT_LINK ethernet
           set_conf_value UPSTREAM_IFACE wlan0; set_conf_value AP_IFACE eth0; set_conf_value AP_VIRTUAL no
           mode=2
           if ip -4 route show default | grep -q 'dev eth0'; then
               warn "the Pi currently reaches the network over Ethernet. This option gives eth0 to the"
               warn "blocked device, which will drop that connection (and any SSH session over it)."
               ask_yn "Continue?" n || die "aborted - connect the Pi to the router over Wi-Fi first"
           fi ;;
        *) die "invalid choice" ;;
    esac
    wizard_rest
}

wizard_wifi_modes() {
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
}

# Everything after the link choice. Uses $mode: 1 = Pi on Ethernet, else Wi-Fi.
wizard_rest() {
    if [[ $mode != 1 ]] && ! wifi_connected wlan0; then
        echo "wlan0 is not connected to your router yet."
        ask rssid "Router Wi-Fi name (SSID)" ""
        read -r -s -p "Router Wi-Fi password: " rpw; echo
        nmcli dev wifi connect "$rssid" password "$rpw" ifname wlan0 \
            || die "could not connect wlan0 to '$rssid'"
    fi

    if [[ $link == 1 ]]; then
        wizard_hidden_network
    fi

    country=$(iw reg get 2>/dev/null | awk '/^country/ { sub(":", "", $2); print $2; exit }')
    [[ $country =~ ^[A-Z]{2}$ && $country != 00 ]] || country=US
    ask country "Wi-Fi country code" "$country"
    set_conf_value AP_COUNTRY "${country^^}"

    if ask_yn "Install Pi-hole for ad/tracker blocking on the gateway?" n; then
        set_conf_value PIHOLE yes
        read -r -s -p "Pi-hole admin password (empty = Pi-hole default): " pw; echo
        set_conf_value PIHOLE_WEB_PASSWORD "$pw"
    fi
}

wizard_hidden_network() {
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

# The hidden network must not reuse the router's subnet (ASUS routers, for
# example, use 192.168.50.x - the default here). Move it if it clashes.
if clash=$(lan_conflict); then
    warn "$LAN_ADDRESS/$LAN_PREFIX clashes with ${clash#* } on ${clash%% *} (your router's network)"
    pick_free_lan || die "no free subnet found - set LAN_ADDRESS/DHCP_* in $PIGW_CONF by hand"
    log "hidden network moved to $LAN_ADDRESS/$LAN_PREFIX (DHCP $DHCP_START-$DHCP_END)"
fi

# --- Files -------------------------------------------------------------------
log "installing files"
install -d -m 755 /usr/local/lib/pi-gateway
install -m 644 "$SRC/lib/common.sh" "$SRC/lib/render.sh" /usr/local/lib/pi-gateway/
install -m 755 "$SRC/lib/net-up" "$SRC/lib/net-down" "$SRC/lib/pihole-setup" "$SRC/lib/watch" \
    "$SRC/lib/usb-gadget" /usr/local/lib/pi-gateway/
install -m 755 "$SRC/bin/pi-gateway" /usr/local/bin/pi-gateway
install -m 644 "$SRC"/systemd/pi-gateway*.service "$SRC"/systemd/pi-gateway*.timer \
    "$SRC/systemd/pi-gateway.target" /etc/systemd/system/
install -d -m 755 /var/lib/pi-gateway

# --- Wi-Fi regulatory domain -------------------------------------------------
if command -v raspi-config >/dev/null; then
    raspi-config nonint do_wifi_country "$AP_COUNTRY" >/dev/null 2>&1 || warn "could not set Wi-Fi country"
fi
rfkill unblock wlan || true

# --- Keep NetworkManager / dhcpcd away from the client-side interface --------
unmanaged="interface-name:$AP_IFACE"
[[ $CLIENT_LINK == usb ]] && unmanaged+=";interface-name:usb0;interface-name:usb1"
if [[ -d /etc/NetworkManager ]]; then
    install -d /etc/NetworkManager/conf.d
    cat >/etc/NetworkManager/conf.d/99-pi-gateway.conf <<EOF
# Managed by pi-gateway: the interface(s) facing the blocked devices.
[keyfile]
unmanaged-devices=$unmanaged
EOF
    systemctl reload NetworkManager 2>/dev/null || true
fi
if [[ -f /etc/dhcpcd.conf ]] && ! grep -q 'pi-gateway' /etc/dhcpcd.conf; then
    printf '\n# pi-gateway\ndenyinterfaces %s\n' "$AP_IFACE" >>/etc/dhcpcd.conf
fi

# --- USB mode: put the USB-C port into device (gadget) mode -------------------
NEED_REBOOT=no
if [[ $CLIENT_LINK == usb ]]; then
    bootcfg=/boot/firmware/config.txt
    [[ -f $bootcfg ]] || bootcfg=/boot/config.txt
    if ! grep -qE '^[[:space:]]*dtoverlay=dwc2' "$bootcfg"; then
        printf '\n# pi-gateway: USB-C port acts as a USB network adapter\n[all]\ndtoverlay=dwc2,dr_mode=peripheral\n' >>"$bootcfg"
        log "enabled USB device mode in $bootcfg"
    fi
    [[ -n $(ls /sys/class/udc 2>/dev/null) ]] || NEED_REBOOT=yes
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
systemctl enable --now pi-gateway-watch.timer >/dev/null
if [[ $NEED_REBOOT == yes ]]; then
    echo
    log "USB device mode needs a reboot. Next steps:"
    echo "  1. sudo poweroff"
    echo "  2. Unplug the Pi's power supply."
    echo "  3. Plug the Pi's USB-C port into the computer with a USB DATA cable."
    echo "     The computer now powers the Pi and sees it as a network adapter."
    echo "  4. After about 30 s, check from the computer: ping $LAN_ADDRESS"
    exit 0
fi

log "starting the gateway"
systemctl restart pi-gateway.target
sleep 3

echo
/usr/local/bin/pi-gateway status || true
echo
case $CLIENT_LINK in
    wifi)
        if ! systemctl is-active --quiet pi-gateway-hostapd.service; then
            warn "the access point did not start - see: journalctl -u pi-gateway-hostapd -b"
        fi
        echo "Scan this on a phone to join the hidden network:"
        /usr/local/bin/pi-gateway qr || true ;;
    usb)
        echo "Plug the Pi's USB-C port into the computer with a USB data cable." ;;
    ethernet)
        echo "Connect the device to the Pi's Ethernet port." ;;
esac
echo
echo "Done. Manage it with:  sudo pi-gateway status | clients | logs"
