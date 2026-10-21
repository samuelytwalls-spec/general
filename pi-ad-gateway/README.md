# Pi Ad-Blocking Gateway

Turns a Raspberry Pi (Raspberry Pi OS Lite) into a filtering middleman:

```
 phones / laptops ──wifi "PiShield"──▶  Raspberry Pi  ──ethernet──▶  your router ──▶ internet
   (own password)                       DHCP + DNS filter + NAT
```

Clients join the Pi's own SSID with its own password. The Pi hands out addresses, answers
their DNS (returning `0.0.0.0` for ad/tracker domains), and forwards everything else to your router.

## Setup

1. Flash **Raspberry Pi OS Lite** (Bookworm+), enable SSH, boot the Pi with ethernet plugged into your router.
2. Copy this folder to the Pi and edit `config/gateway.conf` (SSID, passphrase, country code).
3. `sudo ./install.sh`
4. Join the new SSID from a device, then run `./scripts/test.sh`.

## What it does

| Piece | Role |
|---|---|
| `hostapd` | Broadcasts the new WPA2 network on `wlan0` |
| `dnsmasq` | DHCP for clients + DNS with ~100k+ blocked ad/tracker domains (subdomains included) |
| `nftables` | NAT to the router; redirects any client DNS (port 53) to the Pi so it can't be bypassed; blocks DNS-over-TLS and well-known DNS-over-HTTPS IPs |
| `gateway-blocklist.timer` | Re-downloads blocklists weekly (StevenBlack, AdAway, Yoyo) |

## Customising

- Block more: add domains (one per line) to `/etc/pi-gateway/custom-blocklist.txt`.
- Unblock something broken: add the domain to `/etc/pi-gateway/allowlist.txt`.
- Apply either: `sudo /usr/local/sbin/pi-gateway-update-blocklist`.
- Wifi uplink instead of ethernet: add a USB wifi adapter, join the router with `sudo nmtui`, set `UPLINK_IF="wlan1"`, re-run `install.sh`.

## Limits

- DNS filtering stops most ads, but not ads served from the same domain as content (e.g. YouTube video ads).
- Apps that hardcode their own DoH servers on unlisted IPs can still bypass it.
- The Pi's onboard wifi tops out around 2.4 GHz speeds in AP mode; use ethernet uplink.
- Double NAT: devices behind the Pi can't be reached directly from your main LAN (intended).

## Not included

I deliberately did not add a GoGuardian blocklist. GoGuardian is installed by a school or organisation on devices they manage, and silently cutting its traffic defeats oversight that someone else is responsible for. If you administer the device yourself, `custom-blocklist.txt` takes any domains you want.
