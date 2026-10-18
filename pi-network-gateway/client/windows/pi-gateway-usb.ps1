# Pi Gateway - USB link helper for Windows 10/11
#
# Windows has a built-in driver for the Pi's USB network adapter, so nothing
# needs installing. This script finds the Pi, makes Windows send internet
# traffic through it first, and tests the connection.
#
# Run from an Administrator PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\pi-gateway-usb.ps1
#   powershell -ExecutionPolicy Bypass -File .\pi-gateway-usb.ps1 -NoPriority   (test only)

param([switch]$NoPriority)

function Say($msg, $color = 'Gray') { Write-Host $msg -ForegroundColor $color }

$adapter = Get-NetAdapter -ErrorAction SilentlyContinue |
    Where-Object { $_.InterfaceDescription -match 'RNDIS|Remote NDIS' } |
    Sort-Object Status | Select-Object -First 1

if (-not $adapter) {
    Say 'The Pi was not found as a USB network adapter.' Red
    Say '  - Use a USB DATA cable (many USB-C cables are charge-only).'
    Say '  - Plug into the Pi''s USB-C port (the power port), and wait ~30 s for it to boot.'
    Say '  - Device Manager: if you see "RNDIS" with a yellow mark, right-click > Update driver >'
    Say '    Browse > Let me pick > Network adapters > Microsoft > "Remote NDIS Compatible Device".'
    exit 1
}
Say ("Found: {0} ({1}), status {2}" -f $adapter.Name, $adapter.InterfaceDescription, $adapter.Status) Green
if ($adapter.Status -ne 'Up') {
    Say 'The adapter is not up yet. The Pi may still be booting; try again in a few seconds.' Yellow
    exit 1
}

$cfg = Get-NetIPConfiguration -InterfaceIndex $adapter.ifIndex
$ip = $cfg.IPv4Address.IPAddress
$gw = $cfg.IPv4DefaultGateway.NextHop
if (-not $ip -or $ip -like '169.254.*') {
    Say 'No address from the Pi yet (DHCP). On the Pi run: sudo pi-gateway status' Red
    exit 1
}
Say "Address: $ip   Gateway (the Pi): $gw" Green

if (-not $NoPriority) {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
               ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($isAdmin) {
        # Lower metric = preferred route, so traffic uses the Pi even if
        # another network (e.g. the blocked Wi-Fi) is also connected.
        Set-NetIPInterface -InterfaceIndex $adapter.ifIndex -InterfaceMetric 5
        Rename-NetAdapter -Name $adapter.Name -NewName 'Pi Gateway' -ErrorAction SilentlyContinue
        Say 'Set the Pi as the preferred route.' Green
    } else {
        Say 'Not running as Administrator: skipped making the Pi the preferred route.' Yellow
    }
}

$okGw  = Test-Connection -ComputerName $gw -Count 2 -Quiet
$okNet = Test-Connection -ComputerName 1.1.1.1 -Count 2 -Quiet
$okDns = $false
try { Resolve-DnsName example.com -Server $gw -ErrorAction Stop | Out-Null; $okDns = $true } catch {}

Say ''
Say ("Pi reachable        : {0}" -f $okGw)  ($(if ($okGw)  { 'Green' } else { 'Red' }))
Say ("Internet via the Pi : {0}" -f $okNet) ($(if ($okNet) { 'Green' } else { 'Red' }))
Say ("DNS via the Pi      : {0}" -f $okDns) ($(if ($okDns) { 'Green' } else { 'Red' }))
if ($okGw -and -not $okNet) { Say 'The Pi has no internet itself: on the Pi run  sudo pi-gateway status' Yellow }
