# shellcheck shell=bash disable=SC2034  # config variables are used by the scripts that source this file
# Shared helpers for Pi Gateway. Sourced by net-up, net-down, pi-gateway and
# the installer. Paths can be overridden for testing.

PIGW_CONF=${PIGW_CONF:-/etc/pi-gateway/gateway.conf}
PIGW_RUN=${PIGW_RUN:-/run/pi-gateway}
PIGW_STATE=${PIGW_STATE:-/var/lib/pi-gateway}
PIGW_LIB=${PIGW_LIB:-/usr/local/lib/pi-gateway}

log()  { printf '[pi-gateway] %s\n' "$*" >&2; }
warn() { printf '[pi-gateway] WARNING: %s\n' "$*" >&2; }
die()  { printf '[pi-gateway] ERROR: %s\n' "$*" >&2; exit 1; }

is_yes() { [[ ${1,,} =~ ^(y|yes|true|1|on)$ ]]; }

# --- Config ------------------------------------------------------------------

load_config() {
    [[ -e $PIGW_CONF ]] || die "config not found: $PIGW_CONF (run install.sh first)"
    [[ -r $PIGW_CONF ]] || die "cannot read $PIGW_CONF - run this command with sudo"

    # Defaults for anything missing from older config files.
    CLIENT_LINK=wifi
    UPSTREAM_IFACE=eth0
    AP_IFACE=wlan0 AP_VIRTUAL=no AP_PARENT_IFACE=wlan0
    AP_SSID=PiGateway AP_PASSWORD="" AP_HIDDEN=yes AP_SECURITY=wpa2
    AP_BAND=2.4 AP_CHANNEL=6 AP_COUNTRY=US AP_CLIENT_ISOLATION=no AP_MAX_CLIENTS=16
    LAN_ADDRESS=192.168.50.1 LAN_PREFIX=24
    DHCP_START=192.168.50.10 DHCP_END=192.168.50.200 DHCP_LEASE=12h
    STATIC_LEASES=()
    UPSTREAM_DNS="1.1.1.1 9.9.9.9"
    PIHOLE=no PIHOLE_WEB_PASSWORD=""
    ALLOW_SSH_FROM_AP=yes BLOCK_UPSTREAM_LAN=no

    # shellcheck source=/dev/null
    source "$PIGW_CONF"

    # USB mode: the gadget's interfaces are bridged into one fixed interface.
    if [[ $CLIENT_LINK == usb ]]; then
        AP_IFACE=pigw0 AP_VIRTUAL=no
    fi
}

is_wifi_link() { [[ $CLIENT_LINK == wifi ]]; }

valid_ipv4() {
    local ip=$1 o
    [[ $ip =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
    for o in "${BASH_REMATCH[@]:1}"; do ((10#$o <= 255)) || return 1; done
}

ip2int() {
    local IFS=. a b c d
    read -r a b c d <<<"$1"
    echo $(( (10#$a << 24) | (10#$b << 16) | (10#$c << 8) | 10#$d ))
}

int2ip() {
    local n=$1
    echo "$(( (n >> 24) & 255 )).$(( (n >> 16) & 255 )).$(( (n >> 8) & 255 )).$(( n & 255 ))"
}

prefix2mask() { int2ip $(( (0xFFFFFFFF << (32 - $1)) & 0xFFFFFFFF )); }

# Network address of LAN_ADDRESS/LAN_PREFIX, e.g. 192.168.50.0
lan_network() {
    local mask=$(( (0xFFFFFFFF << (32 - LAN_PREFIX)) & 0xFFFFFFFF ))
    int2ip $(( $(ip2int "$LAN_ADDRESS") & mask ))
}

in_lan() {
    local mask=$(( (0xFFFFFFFF << (32 - LAN_PREFIX)) & 0xFFFFFFFF ))
    (( ($(ip2int "$1") & mask) == ($(ip2int "$LAN_ADDRESS") & mask) ))
}

valid_iface() { [[ $1 =~ ^[A-Za-z0-9_.-]{1,15}$ ]]; }

validate_config() {
    local errors=() e
    [[ $CLIENT_LINK =~ ^(wifi|usb|ethernet)$ ]] || errors+=("CLIENT_LINK must be wifi, usb or ethernet")
    valid_iface "$UPSTREAM_IFACE" || errors+=("UPSTREAM_IFACE is not a valid interface name")
    valid_iface "$AP_IFACE"       || errors+=("AP_IFACE is not a valid interface name")
    [[ $UPSTREAM_IFACE != "$AP_IFACE" ]] || errors+=("UPSTREAM_IFACE and AP_IFACE must differ")
    if is_yes "$AP_VIRTUAL"; then
        valid_iface "$AP_PARENT_IFACE" || errors+=("AP_PARENT_IFACE is not a valid interface name")
    fi

    # Hidden-network settings only matter when devices join over Wi-Fi.
    if is_wifi_link; then
        local ssid_len
        ssid_len=$(printf '%s' "$AP_SSID" | wc -c)
        (( ssid_len >= 1 && ssid_len <= 32 )) || errors+=("AP_SSID must be 1-32 bytes")
        [[ $AP_SSID != *$'\n'* ]] || errors+=("AP_SSID must not contain newlines")
        (( ${#AP_PASSWORD} >= 8 && ${#AP_PASSWORD} <= 63 )) || errors+=("AP_PASSWORD must be 8-63 characters")
        [[ $AP_PASSWORD =~ ^[[:print:]]+$ ]] || errors+=("AP_PASSWORD must be printable ASCII")
        [[ $AP_PASSWORD != "change-this-password" ]] || errors+=("AP_PASSWORD is still the example value")

        [[ $AP_SECURITY =~ ^(wpa2|wpa2-wpa3|wpa3)$ ]] || errors+=("AP_SECURITY must be wpa2, wpa2-wpa3 or wpa3")
        [[ $AP_BAND =~ ^(2\.4|5)$ ]] || errors+=("AP_BAND must be 2.4 or 5")
        [[ $AP_CHANNEL =~ ^[0-9]+$ ]] || errors+=("AP_CHANNEL must be a number")
        if [[ $AP_BAND == 2.4 && $AP_CHANNEL =~ ^[0-9]+$ ]] && ! (( AP_CHANNEL >= 1 && AP_CHANNEL <= 13 )); then
            errors+=("AP_CHANNEL must be 1-13 on 2.4 GHz")
        fi
        if [[ $AP_BAND == 5 && $AP_CHANNEL =~ ^[0-9]+$ ]] && ! (( (AP_CHANNEL >= 36 && AP_CHANNEL <= 48) || (AP_CHANNEL >= 149 && AP_CHANNEL <= 165) )); then
            errors+=("AP_CHANNEL on 5 GHz must be a non-DFS channel (36-48 or 149-165)")
        fi
        [[ $AP_MAX_CLIENTS =~ ^[0-9]+$ ]] && (( AP_MAX_CLIENTS >= 1 && AP_MAX_CLIENTS <= 64 )) \
            || errors+=("AP_MAX_CLIENTS must be 1-64")
    fi
    [[ $AP_COUNTRY =~ ^[A-Z]{2}$ ]] || errors+=("AP_COUNTRY must be a two-letter uppercase code, e.g. US, GB, DE")

    valid_ipv4 "$LAN_ADDRESS" || errors+=("LAN_ADDRESS is not an IPv4 address")
    [[ $LAN_PREFIX =~ ^[0-9]+$ ]] && (( LAN_PREFIX >= 16 && LAN_PREFIX <= 29 )) \
        || errors+=("LAN_PREFIX must be 16-29")
    if valid_ipv4 "$LAN_ADDRESS" && [[ $LAN_PREFIX =~ ^[0-9]+$ ]]; then
        for e in DHCP_START DHCP_END; do
            if ! valid_ipv4 "${!e}"; then errors+=("$e is not an IPv4 address")
            elif ! in_lan "${!e}"; then errors+=("$e is outside $LAN_ADDRESS/$LAN_PREFIX")
            fi
        done
        if valid_ipv4 "$DHCP_START" && valid_ipv4 "$DHCP_END" \
           && (( $(ip2int "$DHCP_START") > $(ip2int "$DHCP_END") )); then
            errors+=("DHCP_START must not be after DHCP_END")
        fi
    fi
    [[ $DHCP_LEASE =~ ^[0-9]+[mhd]?$|^infinite$ ]] || errors+=("DHCP_LEASE must look like 30m, 12h, 1d")

    local entry mac ip name
    for entry in "${STATIC_LEASES[@]}"; do
        IFS=, read -r mac ip name <<<"$entry"
        [[ $mac =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]] || errors+=("STATIC_LEASES: bad MAC in '$entry'")
        valid_ipv4 "$ip" && in_lan "$ip" || errors+=("STATIC_LEASES: bad or out-of-range IP in '$entry'")
        [[ -z $name || $name =~ ^[A-Za-z0-9-]+$ ]] || errors+=("STATIC_LEASES: name must be letters, digits, '-' in '$entry'")
    done

    for e in $UPSTREAM_DNS; do
        valid_ipv4 "$e" || errors+=("UPSTREAM_DNS: '$e' is not an IPv4 address")
    done
    if is_yes "$PIHOLE" && [[ -z $UPSTREAM_DNS ]]; then
        errors+=("UPSTREAM_DNS must be set when PIHOLE=yes")
    fi

    if ((${#errors[@]})); then
        for e in "${errors[@]}"; do printf '[pi-gateway] config error: %s\n' "$e" >&2; done
        die "fix $PIGW_CONF and try again"
    fi
}

# Rewrite KEY=... in the config file (or append it) with a safely quoted value.
set_conf_value() {
    local key=$1 value=$2 line tmp
    line="$key=$(printf '%q' "$value")"
    tmp=$(mktemp "${PIGW_CONF}.XXXXXX")
    KEY=$key LINE=$line awk '
        BEGIN { k = ENVIRON["KEY"]; l = ENVIRON["LINE"]; done = 0 }
        index($0, k "=") == 1 { if (!done) print l; done = 1; next }
        { print }
        END { if (!done) print l }
    ' "$PIGW_CONF" >"$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$PIGW_CONF"
}

# Do two networks overlap?  subnets_overlap 192.168.50.1/24 192.168.50.38/24
subnets_overlap() {
    local a=${1%/*} pa=${1#*/} b=${2%/*} pb=${2#*/}
    local p=$(( pa < pb ? pa : pb ))
    local mask=$(( (0xFFFFFFFF << (32 - p)) & 0xFFFFFFFF ))
    (( ($(ip2int "$a") & mask) == ($(ip2int "$b") & mask) ))
}

# Print the first address on another interface that clashes with the AP subnet
# (for example the router's network also being 192.168.50.x). Ignores AP_IFACE.
lan_conflict() {
    local ifc cidr
    while read -r ifc cidr; do
        [[ $ifc == "$AP_IFACE" || $ifc == lo ]] && continue
        if subnets_overlap "$LAN_ADDRESS/$LAN_PREFIX" "$cidr"; then
            echo "$ifc $cidr"
            return 0
        fi
    done < <(ip -4 -o addr show 2>/dev/null | awk '{ print $2, $4 }')
    return 1
}

# Move LAN_ADDRESS/DHCP_* to the first candidate /24 that clashes with nothing.
pick_free_lan() {
    local base
    for base in 192.168.50 10.50.0 172.20.50 192.168.77 10.77.0; do
        LAN_ADDRESS=$base.1 LAN_PREFIX=24
        if ! lan_conflict >/dev/null; then
            set_conf_value LAN_ADDRESS "$base.1"
            set_conf_value LAN_PREFIX 24
            set_conf_value DHCP_START "$base.10"
            set_conf_value DHCP_END "$base.200"
            load_config
            return 0
        fi
    done
    return 1
}

# Frequency in MHz -> channel number.
freq2chan() {
    local f=$1
    if (( f == 2484 )); then echo 14
    elif (( f >= 2412 && f <= 2472 )); then echo $(( (f - 2407) / 5 ))
    elif (( f >= 5000 && f <= 5900 )); then echo $(( (f - 5000) / 5 ))
    else return 1
    fi
}
