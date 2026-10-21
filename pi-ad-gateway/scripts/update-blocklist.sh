#!/usr/bin/env bash
# Builds the dnsmasq blocklist from public ad/tracker lists plus your own files,
# then reloads dnsmasq. Runs weekly via gateway-blocklist.timer.
set -euo pipefail

DIR=/etc/pi-gateway
OUT=$DIR/blocklist.conf
TMP=$(mktemp)
trap 'rm -f "$TMP" "$TMP".*' EXIT

# Public ad / tracker / malware lists (hosts format).
SOURCES=(
  "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts"
  "https://adaway.org/hosts.txt"
  "https://pgl.yoyo.org/adservers/serverlist.php?hostformat=hosts&showintro=0&mimetype=plaintext"
)

for url in "${SOURCES[@]}"; do
  curl -fsSL --max-time 60 "$url" >> "$TMP.raw" || echo "warn: failed to fetch $url" >&2
done

# Your own entries: one domain per line in /etc/pi-gateway/custom-blocklist.txt
[ -f "$DIR/custom-blocklist.txt" ] && sed 's/^/0.0.0.0 /' "$DIR/custom-blocklist.txt" >> "$TMP.raw"

# Extract domains, drop comments/localhost, remove allowlisted ones, dedupe.
awk '$1 ~ /^(0\.0\.0\.0|127\.0\.0\.1)$/ && $2 !~ /^(localhost|0\.0\.0\.0|local|broadcasthost)/ {print tolower($2)}' "$TMP.raw" \
  | tr -d '\r' | sort -u > "$TMP.domains"

if [ -f "$DIR/allowlist.txt" ]; then
  grep -vxFf <(grep -v '^\s*\(#\|$\)' "$DIR/allowlist.txt") "$TMP.domains" > "$TMP.filtered" || true
  mv "$TMP.filtered" "$TMP.domains"
fi

# address=/domain/ also blocks every subdomain. Answer 0.0.0.0 / :: instantly.
sed 's#.*#address=/&/0.0.0.0#' "$TMP.domains" > "$TMP"

if [ ! -s "$TMP" ]; then
  echo "error: empty blocklist, keeping the previous one" >&2
  exit 1
fi

install -m 644 "$TMP" "$OUT"
echo "blocklist: $(wc -l < "$OUT") domains"

if systemctl is-active --quiet dnsmasq; then
  dnsmasq --test -C /etc/dnsmasq.conf && systemctl restart dnsmasq
fi
