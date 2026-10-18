# Pi Gateway: a hidden Wi-Fi network routed through a Raspberry Pi 5

This turns a Raspberry Pi 5 into a small router that sits between your router
and your devices. The Pi joins the router as a normal client, then runs its own
**hidden, password-protected Wi-Fi network**. Devices on that network get
internet through the Pi using NAT, so the router only ever sees the Pi. A device
the router refuses to serve (for example, one blocked by MAC address or
missing from an allow-list) works through the Pi. **Pi-hole** ad blocking for
that network is optional.

```
                 router's network                      Pi Gateway network (hidden SSID, WPA2)
 ┌────────┐   eth0 or wlan0    ┌──────────────────────────────────┐   wlan0 / wlan1 / uap0   ┌────────────────┐
 │ Router ├───────────────────►│ Raspberry Pi 5                   │◄────────────────────────►│ blocked device │
 └────────┘  Pi gets a normal  │  hostapd   hidden AP, WPA2/WPA3  │  192.168.50.10-200       └────────────────┘
             address from the  │  dnsmasq   DHCP (+ DNS)          │                          ┌────────────────┐
             router            │  nftables  NAT + firewall        │◄────────────────────────►│ other devices  │
                               │  Pi-hole   DNS filter (optional) │                          └────────────────┘
                               └──────────────────────────────────┘
```

Only use this on networks you own or have permission to extend. If a school,
employer or ISP blocks a device on purpose, routing around the block may break
their rules.

## Contents

- [Why Linux and not bare metal](#why-linux-and-not-bare-metal)
- [Hardware and setup modes](#hardware-and-setup-modes)
- [Install](#install)
- [Connect a device to the hidden network](#connect-a-device-to-the-hidden-network)
- [Pi-hole](#pi-hole)
- [Day-to-day commands](#day-to-day-commands)
- [Configuration reference](#configuration-reference)
- [How it works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [Uninstall](#uninstall)
- [Testing without a Pi](#testing-without-a-pi)

## Why Linux and not bare metal

A bare-metal build (no operating system) is not workable here, for three reasons:

1. **Wi-Fi.** The Pi 5's Wi-Fi chip (Infineon/Cypress CYW43455) is reached
   through the RP1 southbridge over SDIO and runs Broadcom's closed firmware.
   Using it as an access point needs that firmware plus a full 802.11 stack:
   WPA2/WPA3 handshakes, hidden-SSID probe handling and power save. No
   bare-metal stack does this on a Pi 5. On Linux, `brcmfmac` and `hostapd`
   handle it.
2. **Routing.** NAT, connection tracking, DHCP and DNS are each large,
   security-sensitive pieces of software. Linux has proven versions of all of
   them: nftables, dnsmasq and conntrack.
3. **Pi-hole** runs only on Linux.

The closest you can get to "bare metal" is a minimal headless system. Use
**Raspberry Pi OS Lite (64-bit)** with no desktop. The gateway adds only
hostapd, dnsmasq and an nftables table, and it idles at a few MB of RAM.

## Hardware and setup modes

| Mode | Pi → router | Hidden network on | Notes |
|---|---|---|---|
| **1. Ethernet uplink** (recommended) | Ethernet cable (`eth0`) | built-in Wi-Fi (`wlan0`) | Fastest and most reliable. Any channel and band. |
| **2. USB adapter** | built-in Wi-Fi (`wlan0`) | USB Wi-Fi adapter (`wlan1`) | No cable needed. The adapter must support AP mode (check with `iw list`: "Supported interface modes: AP"). Adapters with in-kernel drivers, such as MediaTek MT7612U and MT7921AU, work well. |
| **3. Single radio** | built-in Wi-Fi (`wlan0`) | virtual AP on the same chip (`uap0`) | No extra hardware. The AP must use the router's channel and is limited to 20 MHz, so it is slower. The router's channel must not be a 5 GHz DFS (radar) channel, 52–144: set its 5 GHz channel to 36–48 or 149–165, or join its 2.4 GHz network. If the router changes channel, the gateway follows within about a minute. |

You also need a microSD card (8 GB or more) or an NVMe SSD, and the official
27 W USB-C power supply.

## Install

1. Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. In the
   Imager settings, enable SSH and set a username. In modes 2 and 3, also
   enter your router's Wi-Fi details there.
2. Boot the Pi, SSH in, and copy this folder onto it, for example with
   `git clone` of this repository or `scp -r pi-network-gateway pi@<pi-ip>:`.
3. Run the installer:

   ```bash
   cd pi-network-gateway
   sudo ./install.sh
   ```

   The wizard asks for:
   - the setup mode (1/2/3). It suggests one based on whether Ethernet is
     plugged in and whether a USB adapter is present
   - your router's Wi-Fi name and password, if the Pi is not connected yet
     (modes 2 and 3)
   - the **hidden network name** and **password**. Leave the password empty
     to generate a strong one
   - whether to hide the SSID (default: yes)
   - the band (2.4 or 5 GHz) and the Wi-Fi country code
   - whether to install **Pi-hole**

   The installer then installs packages, writes `/etc/pi-gateway/gateway.conf`,
   starts the services and prints a QR code for joining the network.

**Non-interactive install:** copy `config/gateway.conf.example`, edit it, then
run `sudo ./install.sh --config my-gateway.conf`.

> In mode 1, if you are connected to the Pi over **Wi-Fi**, that SSH session
> drops when `wlan0` becomes the access point. Plug in Ethernet first and SSH
> to the Pi's Ethernet address. The installer warns you about this.

The gateway starts automatically on every boot (`pi-gateway.target`).

## Connect a device to the hidden network

A hidden network does not appear in Wi-Fi lists, so you add it by hand:

- **Phones:** run `sudo pi-gateway qr` on the Pi and scan the code with the
  camera. The code includes the hidden flag.
- **Windows:** Settings → Network → Wi-Fi → Manage known networks → Add network.
  Enter the SSID, choose WPA2-Personal, enter the password, and tick
  "Connect even if this network is not broadcasting".
- **macOS:** Wi-Fi menu → Other Networks → Other… and enter the SSID, security
  and password.
- **Linux:** `nmcli dev wifi connect "<SSID>" password "<password>" hidden yes`
- **Consoles, TVs and similar:** look for "Manual setup" or "Add network" in
  the network settings.

The device gets an address in `192.168.50.x` and uses the Pi (`192.168.50.1`)
as its gateway and DNS server. If your router already uses `192.168.50.x`, the
installer picks a free subnet instead, such as `10.50.0.x`, and
`sudo pi-gateway status` shows which one.

## Pi-hole

Turn it on during install, or at any time later:

```bash
sudo pi-gateway pihole on     # installs Pi-hole if needed, then makes it the DNS server for the network
sudo pi-gateway pihole off    # back to plain dnsmasq DNS (Pi-hole stays installed but stopped)
```

With Pi-hole on:
- dnsmasq only hands out addresses (DHCP). Pi-hole answers DNS on
  `192.168.50.1`.
- Pi-hole uses `UPSTREAM_DNS` from the config as its upstream resolvers. Its
  own DHCP server stays off.
- The admin page is at **http://192.168.50.1/admin**. You can open it from any
  device on the hidden network, or from your router's network at the Pi's
  address there. Set `PIHOLE_WEB_PASSWORD` in the config, or run
  `sudo pihole setpassword`.

Pi-hole installs with its official unattended installer from
`https://install.pi-hole.net`.

## Day-to-day commands

```
sudo pi-gateway status       services, whether the AP is broadcasting, addresses, channel, clients
sudo pi-gateway clients      connected devices: MAC, IP, hostname, signal
sudo pi-gateway qr           QR code for joining the hidden network
sudo pi-gateway set-password change the Wi-Fi password (prompts)
sudo pi-gateway set-ssid X   rename the network
sudo pi-gateway hidden on|off
sudo pi-gateway kick MAC     disconnect a device
sudo pi-gateway pihole on|off
sudo pi-gateway config       edit gateway.conf, validate it, restart
sudo pi-gateway restart | start | stop
sudo pi-gateway logs         follow the logs of all gateway services
```

## Configuration reference

All settings live in `/etc/pi-gateway/gateway.conf` (root-only, mode 600,
because it holds the password). The example file
[`config/gateway.conf.example`](config/gateway.conf.example) documents every
option. The main ones are:

| Setting | Default | Meaning |
|---|---|---|
| `UPSTREAM_IFACE` | `eth0` | Interface the Pi uses to reach the router |
| `AP_IFACE` / `AP_VIRTUAL` | `wlan0` / `no` | Interface for the hidden network. Use `uap0` with `AP_VIRTUAL=yes` for single-radio mode |
| `AP_SSID`, `AP_PASSWORD` | – | Network name (1–32 bytes) and password (8–63 characters) |
| `AP_HIDDEN` | `yes` | Hide the SSID from beacons |
| `AP_SECURITY` | `wpa2` | `wpa2`, `wpa2-wpa3` (transition) or `wpa3`. WPA3/SAE in AP mode depends on the Pi's Wi-Fi firmware; if clients can't join, go back to `wpa2` |
| `AP_BAND`, `AP_CHANNEL` | `2.4`, `6` | 5 GHz allows only non-DFS channels: 36–48 and 149–165 |
| `AP_COUNTRY` | `US` | Regulatory domain. Set your own country |
| `AP_CLIENT_ISOLATION` | `no` | Stop devices on the hidden network from reaching each other |
| `LAN_ADDRESS`, `LAN_PREFIX`, `DHCP_*` | `192.168.50.1/24` | Subnet for the hidden network. It must not overlap the router's subnet |
| `STATIC_LEASES` | – | Fixed IPs: `"MAC,IP,name"` |
| `UPSTREAM_DNS` | `1.1.1.1 9.9.9.9` | Upstream resolvers. Leave empty to use the router's DNS |
| `PIHOLE` | `no` | Use Pi-hole for DNS |
| `ALLOW_SSH_FROM_AP` | `yes` | Allow SSH to the Pi from the hidden network |
| `BLOCK_UPSTREAM_LAN` | `no` | Block hidden-network devices from the router's LAN, so they get internet only |

Apply changes with `sudo pi-gateway restart`. A bad value is rejected with a
message naming the setting.

## How it works

```
pi-gateway.target
├── pi-gateway-net.service      lib/net-up / lib/net-down
│     • validates gateway.conf
│     • single-radio: waits for wlan0's link, reads its channel, creates uap0
│       (pi-gateway-watch.timer restarts everything if the router changes channel)
│     • sets 192.168.50.1/24 on the AP interface, enables IPv4 forwarding
│     • loads the nftables table "inet pigw"
│     • renders /run/pi-gateway/{hostapd.conf,dnsmasq.conf,rules.nft}
├── pi-gateway-hostapd.service  hostapd: hidden SSID (ignore_broadcast_ssid=1), WPA2/WPA3
└── pi-gateway-dhcp.service     dnsmasq: DHCP, plus DNS unless Pi-hole is on
```

- **NAT:** traffic from the hidden network leaves through the upstream interface
  with the Pi's address (`masquerade`). The router sees a single device, the
  Pi, so it does not need to know about or allow the devices behind it.
- **Firewall:** the gateway uses its own nftables table (`inet pigw`) and
  leaves other rules alone. Hidden-network devices can:
  - reach the internet through the upstream interface
  - reach the Pi only for DHCP, DNS and ping, plus SSH and Pi-hole's web page
    if those are enabled

  Nothing on the router's side can start a connection into the hidden
  network. IPv6 forwarding from the hidden network is blocked, so devices
  use IPv4 only.
- **NetworkManager** stays in charge of the uplink. The installer marks only
  the AP interface as unmanaged
  (`/etc/NetworkManager/conf.d/99-pi-gateway.conf`).
- The stock `hostapd.service` and `dnsmasq.service` are disabled so they do
  not compete for the radio or ports 53/67.

Files:

| Path | What |
|---|---|
| `/etc/pi-gateway/gateway.conf` | Your configuration |
| `/usr/local/lib/pi-gateway/` | `common.sh`, `render.sh`, `net-up`, `net-down`, `pihole-setup` |
| `/usr/local/bin/pi-gateway` | CLI |
| `/etc/systemd/system/pi-gateway*.{service,target}` | Services |
| `/run/pi-gateway/` | Generated configs, rebuilt on every start |
| `/var/lib/pi-gateway/dnsmasq.leases` | DHCP leases |

## Troubleshooting

| Symptom | What to check |
|---|---|
| `pi-gateway-hostapd` fails | `journalctl -u pi-gateway-hostapd -b`. Typical causes: wrong `AP_COUNTRY`, a channel not allowed in your country, or a USB adapter without AP mode. Try `AP_BAND=2.4` and `AP_CHANNEL=6`. |
| Device can't find the network | It is hidden: add it by hand or use the QR code. Check the SSID's spelling and case. To rule out the hidden setting, try `sudo pi-gateway hidden off`. |
| Device connects but has no internet | `sudo pi-gateway status`: the upstream needs an address and there must be a default route. Run `sudo nft list table inet pigw`. If a firewall such as ufw is installed with a forward policy of DROP, allow forwarding from the AP interface to the upstream interface. |
| Connects, gets no IP | `journalctl -u pi-gateway-dhcp -b`. Check whether another DHCP or DNS server (stock dnsmasq, Pi-hole DHCP) is running. |
| Single-radio mode: `pi-gateway-net` fails with "DFS (radar) channel", or AP state is `COUNTRY_UPDATE`/`DFS` | The router is on a 5 GHz radar channel (52–144), where the Pi cannot run an access point. In the router's settings, pick 5 GHz channel 36, 40, 44, 48 or 149–165 and turn off automatic selection of DFS channels. On ASUS routers this is under Wireless → General: untick "Auto select channel including DFS channels". Or connect `wlan0` to the router's 2.4 GHz network. The gateway restarts by itself once the channel changes. |
| Single-radio mode stops working after a while | The router changed channel. `pi-gateway-watch.timer` restarts the gateway within a minute; check with `systemctl status pi-gateway-watch.timer`. A fixed channel on the router avoids the brief outage. |
| `pi-gateway-net` fails with "overlaps … on wlan0/eth0" | The hidden network uses the same subnet as your router. ASUS routers, for example, use `192.168.50.x`, which is also the default here. Re-run `sudo ./install.sh` and it moves the hidden network to a free subnet automatically, or change `LAN_ADDRESS` and `DHCP_*` by hand, for example to `10.50.0.x`. |
| Pi-hole page doesn't load | `systemctl status pihole-FTL`. Open `http://192.168.50.1/admin` from a device on the hidden network. |

## Uninstall

```bash
sudo ./uninstall.sh           # keeps /etc/pi-gateway
sudo ./uninstall.sh --purge   # also deletes config and leases
sudo pihole uninstall         # if you installed Pi-hole and want it gone
```

## Testing without a Pi

`tests/render-test.sh` builds every config for several setups (Ethernet with
WPA2, 5 GHz with Pi-hole and WPA2/WPA3, single radio, static leases) and
checks the output. It also checks that invalid configs are rejected. When run
as root it validates the nftables rules with `nft -c`, and it checks the
dnsmasq configs with `dnsmasq --test`.

```bash
sudo bash tests/render-test.sh
```
