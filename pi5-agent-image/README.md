# PiAgent image for Raspberry Pi 5

A build system for a flashable Raspberry Pi 5 (8 GB) image based on **Raspberry
Pi OS Lite (Bookworm, arm64)**, built with the official
[pi-gen](https://github.com/RPi-Distro/pi-gen) in Docker. The image is CLI-only
with a minimal set of services and contains:

| Component | What it does |
|---|---|
| **llama.cpp** (`b11559`, cross-compiled for Cortex-A76) | `llama-server` on `127.0.0.1:8081` running **Phi-4-mini-instruct Q4_K_M** with a **4k context**, plus a second instance on `127.0.0.1:8082` for embeddings |
| **PiAgent** (Python, stdlib + sqlite-vec) | A tool-calling agent with a `shell` tool. Runs as the unprivileged `piagent` user and **asks for confirmation before destructive commands** |
| **RAG** | `bge-small-en-v1.5` embeddings + **sqlite-vec** over `/srv/docs`, re-indexed incrementally |
| **HTTP API** | HTTPS on port 8443, **LAN-only** (app check + nftables), bearer token in **`/etc/piagent/token`** |
| **Companion client** | `client/piagent_client.py` for Linux, Windows and macOS: accepts **vetted, signed** commands from the Pi |

```
 LAN                                   Raspberry Pi 5 (Raspberry Pi OS Lite, Bookworm arm64)
 ┌──────────────┐  HTTPS :8443 + token  ┌───────────────────────────────────────────────────────┐
 │ you: curl /  ├──────────────────────►│ piagent-api (user piagent, systemd sandbox)           │
 │ piagent CLI  │                       │   agent loop ── shell tool (bash, as piagent)          │
 └──────────────┘                       │      │      └─ safety check: safe / confirm / deny     │
 ┌──────────────┐  long-poll, signed    │      ├─ search_docs ── sqlite-vec (/var/lib/piagent)   │
 │ laptop/desk  │◄─────────────────────►│      └─ remote_exec ── device queue (HMAC-signed)      │
 │ piagent_     │  commands + results   │  llama-server :8081  Phi-4-mini Q4_K_M, ctx 4096      │
 │ client.py    │                       │  llama-server :8082  bge-small-en-v1.5 (embeddings)   │
 └──────────────┘                       │  nftables: SSH + API from private ranges only          │
                                        └───────────────────────────────────────────────────────┘
```

## Contents

- [Build the image](#build-the-image) (Linux or Windows/WSL2)
- [Flash the SD card / SSD](#flash-the-image)
- [First boot](#first-boot)
- [Using the agent](#using-the-agent)
- [Documents and search (RAG)](#documents-and-search-rag)
- [HTTP API](#http-api)
- [Companion client for remote devices](#companion-client-for-remote-devices)
- [Security model](#security-model)
- [Services, files and tuning](#services-files-and-tuning)
- [Troubleshooting](#troubleshooting)
- [Repository layout and tests](#repository-layout-and-tests)

---

## Build the image

### What you need

- An x86-64 or arm64 machine running **Linux**, or **Windows 10/11 with WSL2**.
- **Docker**: Docker Desktop with WSL integration, or Docker Engine.
- **qemu-user-static** and **binfmt-support** on x86-64 hosts (to run arm64 code during the build).
- About **35 GB free disk** and 8 GB RAM. The build takes roughly 45 to 120 minutes, mostly pi-gen running under emulation.
- Internet access to: `github.com` (pi-gen, llama.cpp), `deb.debian.org` and
  `archive.raspberrypi.com` (packages), `huggingface.co` and its download CDN
  (models), `pypi.org` / `files.pythonhosted.org` (the sqlite-vec wheel), and
  Docker Hub (`debian:bookworm`).

### On Windows (WSL2)

1. Install WSL with Ubuntu (PowerShell as administrator), then reboot:
   ```powershell
   wsl --install -d Ubuntu-24.04
   ```
2. Install **Docker Desktop**, and enable *Settings → Resources → WSL integration*
   for your Ubuntu distribution. Alternatively, install Docker Engine inside
   Ubuntu (`sudo apt install docker.io`, then `sudo usermod -aG docker $USER`)
   after enabling systemd in `/etc/wsl.conf` (`[boot]` → `systemd=true`).
3. In the Ubuntu terminal:
   ```bash
   sudo apt update
   sudo apt install -y git curl python3 qemu-user-static binfmt-support
   ```
4. Clone **into the Linux file system** (for example `~/`), not `/mnt/c/...`.
   Windows drives are slow and lose Linux permissions; `build.sh` refuses to run there.

Then follow the Linux steps.

### On Linux

```bash
sudo apt install -y git curl python3 qemu-user-static binfmt-support docker.io   # Debian/Ubuntu
git clone <this repository> && cd <repo>/pi5-agent-image

# Optional: private settings, read after config/build.conf and ignored by git
cat > config/build.local.conf <<'EOF'
FIRST_USER_PASS='choose-a-password'
PUBKEY_SSH_FIRST_USER='ssh-ed25519 AAAA... you@laptop'
# WPA_COUNTRY=GB
# WIFI_SSID='MyNetwork'
# WIFI_PSK='my-wifi-password'
EOF

./build.sh
```

`build.sh` runs these steps, and caches each result in `cache/`:

1. Clones **pi-gen** at tag `2026-10-06-raspios-bookworm-arm64` and **llama.cpp**
   at `b11559`, and checks that both match the pinned commits.
2. **Cross-compiles llama.cpp** in a `debian:bookworm` container with
   `-march=armv8.2-a+dotprod+fp16` (Cortex-A76), statically linking ggml/llama.
   It refuses binaries that need a newer glibc or libstdc++ than Bookworm has.
3. Downloads **Phi-4-mini-instruct Q4_K_M** and **bge-small-en-v1.5 Q8_0**,
   checks they are GGUF files, verifies pinned SHA-256 hashes, and writes `SHA256SUMS`.
4. Downloads the **sqlite-vec** manylinux aarch64 wheel and verifies its pinned hash.
5. Writes the pi-gen `config` and runs `build-docker.sh` with
   `STAGE_LIST="stage0 stage1 stage2 stage-piagent"`, so the base is the official
   Lite build. The custom stage is mounted from `cache/artifacts` read-only.
6. Moves the result to **`deploy/<date>-raspios-bookworm-arm64-pi5-piagent.img`**
   and writes a `.sha256` file next to it.

If `FIRST_USER_PASS` is empty, the build generates a password, prints it, and
saves it as `deploy/*-password.txt`.

Useful options:

| Command | Effect |
|---|---|
| `./build.sh --continue` | resume a failed pi-gen run in its existing container |
| `./build.sh --clean` | delete the pi-gen container and checkout, then rebuild |
| `./build.sh --fetch-only` | only download/compile the inputs |
| `./build.sh -c other.conf` | use another config file |
| `DEPLOY_COMPRESSION=xz` (in config) | write `.img.xz`, about 3× smaller; Raspberry Pi Imager flashes it directly |
| `BUILDER_DOCKER_OPTS` / `PIGEN_DOCKER_OPTS` | extra `docker run` options, e.g. `--network host` for a proxy on localhost |

**Behind an HTTPS proxy.** If outbound traffic must go through an HTTPS proxy,
possibly one that inspects TLS with its own CA, set these in
`config/build.local.conf`:

```bash
BUILD_HTTPS_PROXY=http://proxy.example:3128
BUILD_CA_FILE=/path/to/proxy-ca-bundle.pem    # only for TLS-inspecting proxies
```

The build containers then use host networking, fetch the Debian and Raspberry
Pi archives over HTTPS through the proxy, and trust the CA. `build.sh` applies
this to its pi-gen checkout in `.build/` only, and the export stage restores
the stock apt sources, so the image contains no proxy settings or build CA.
Model and wheel downloads run on the host and use the usual `https_proxy`
environment variable.

**Model hashes.** The model URLs are mutable Hugging Face paths, so
`config/build.conf` pins the SHA-256 of both files and the build stops if a
download does not match. If you switch models, update `CHAT_MODEL_SHA256` /
`EMBED_MODEL_SHA256`, or empty them to trust the first download (its hash is
printed so you can pin it). To build offline, put the `.gguf` files into
`cache/artifacts/models/` yourself and `build.sh` will use them.

**Other models.** Change `CHAT_MODEL_URL`/`CHAT_MODEL_FILE` (any GGUF that
llama.cpp supports) or the embedding model. If the embedding model is not a BGE
model, also change `EMBED_POOLING` in `/etc/piagent/llama.env` on the Pi
(`mean` for nomic/MiniLM). The index rebuilds itself when the model name changes.

---

## Flash the image

The image fits on a 16 GB card. For speed, use a 32 GB+ A2-class card or an NVMe SSD.

- **Raspberry Pi Imager** (Windows/macOS/Linux): *Choose OS → Use custom* → pick
  the `.img` (or `.img.xz`), choose the storage, then *Write*. You do not need
  Imager's "OS customisation", because the user, SSH and Wi-Fi come from the
  build config. If you do use it, the settings you enter there take precedence.
- **Linux, command line** (check the device name first with `lsblk`; this erases it):
  ```bash
  sha256sum -c deploy/*.img.sha256
  sudo dd if=deploy/<image>.img of=/dev/sdX bs=4M conv=fsync status=progress
  ```
  With `bmaptool` installed, `sudo bmaptool copy deploy/<image>.img /dev/sdX` is faster.
- **From WSL2**: WSL cannot see USB card readers by default. Use Raspberry Pi
  Imager on Windows and open the image via `\\wsl$\Ubuntu-24.04\home\<you>\...\deploy\`.

**Optional: choose the API token before the first boot.** After flashing, the
FAT `bootfs` partition is visible on any OS. Create `piagent-token.txt` there
with a random string of at least 32 characters, for example from
`openssl rand -base64 32`. On first boot it becomes `/etc/piagent/token` and
the file is deleted. Without it, a random token is generated.

## First boot

1. Insert the card, connect **Ethernet** (or use the Wi-Fi configured at build
   time), and power on with the official 27 W USB-C supply.
2. On first boot the root filesystem grows to fill the card, then
   `piagent-firstboot.service` creates:
   - the API token in `/etc/piagent/token` (`root:piagent`, mode `0640`);
   - a self-signed TLS certificate in `/etc/piagent/tls/`, with its SHA-256
     fingerprint in `/etc/piagent/tls/fingerprint.txt`.

   Every device gets its own secrets; none are baked into the image.
3. After 1–2 minutes, log in from a machine on the same LAN:
   ```bash
   ssh pi@piagent.local      # or the IP shown by your router; user/password from the build config
   ```
   The login banner shows the API URL and TLS fingerprint.
4. Check that everything is up. Loading the model takes about 10–30 seconds after boot:
   ```bash
   piagent status            # chat_model_ready / embed_model_ready should be true
   systemctl status llama-server llama-embed piagent-api
   sudo cat /etc/piagent/token
   ```
5. Talk to the agent:
   ```bash
   piagent chat
   ```
6. Recommended: change the password (`passwd`) unless you set your own, and
   run `sudo apt update && sudo apt full-upgrade` now and then. Automatic apt
   timers are disabled to keep the system quiet.

The login user is in the `piagent` group, so it can read the token and use the
`piagent` command. Log out and back in once if `piagent` says it cannot read the token.

---

## Using the agent

An example session (the answers depend on your system and the model):

```text
$ piagent chat
you> how hot is the CPU and how much disk is free?
  -> shell: vcgencmd measure_temp
  -> shell: df -h /

The CPU is at ... and the root filesystem has ... free.

you> delete the old logs in ~/work/logs
  -> shell: rm -r logs

  The agent wants to run a command that needs your approval:
      rm -r logs
    on:  this Pi (as user 'piagent')
    why: rm: deletes files
  Run it? [y/N] n
  rejected: rm -r logs

I did not delete the logs because you declined.
```

- `piagent ask "question"` asks one question; add `--no-confirm` to reject every
  destructive command automatically, which is useful in scripts.
- `piagent chat -v` also shows tool output and the model's reasoning.
- `piagent search "query"` searches documents directly.

**How tool calling works.** On every step the model must answer with one JSON
object, `{"thought", "tool", "input", "device"}`. llama-server enforces this by
compiling a JSON schema into a grammar, so even a 3.8B model always produces
valid calls. Tools: `shell`, `search_docs`, `list_devices`, `remote_exec`,
`final_answer`. Tool output is fed back as an observation until
`final_answer`, or until 8 steps, after which an answer is forced.

**What counts as destructive.** `piagent/piagent/safety.py` parses the command
line, including pipes, `;`, `&&`, redirections, `bash -c`, `xargs`, `find -exec`,
and wrappers like `nohup`, `timeout` and `env`. It sorts commands into:

- **safe**: runs immediately. Read-only commands such as `ls`, `df`, `cat`, `grep`, `systemctl status`, `git log`.
- **confirm**: needs a human yes. Covers `rm`, `mv`, `cp`, `dd`, `chmod`/`chown`,
  `kill`, output redirection into files, `sed -i`, `systemctl restart/stop/...`,
  package installs and removals, `git reset/clean/push`, network changes,
  `reboot`/`shutdown`, interpreters running code (`python3 -c`, scripts),
  command substitution, and anything it cannot parse.
- **deny**: never runs. Covers `sudo`/`su`, disk formatting or partitioning,
  writes to block devices, recursive deletes of `/` or system directories, and fork bombs.

Add your own regexes with `extra_confirm_patterns` / `extra_deny_patterns` in
`/etc/piagent/piagent.toml`. The classifier is a guard rail, not the security
boundary. The boundary is that the agent runs as the unprivileged `piagent`
user under systemd with `NoNewPrivileges`, an empty capability set, and a
read-only `/usr`, `/boot` and `/etc`. Even an approved command cannot become root.

## Documents and search (RAG)

- Copy files into **`/srv/docs`**, for example
  `scp -r ./manuals pi@piagent.local:/srv/docs/`. The directory is
  group-writable for `piagent`.
- Supported formats: `.txt .md .rst .adoc .html .csv .tsv .json .yaml .toml .ini
  .conf .cfg .log .py .sh`, plus `.pdf` after `sudo apt install poppler-utils`.
- `piagent-index.timer` indexes 3 minutes after boot and every 30 minutes
  after that. Only new or changed files are re-embedded, and deleted files are
  removed. To index now, run `piagent index`; `piagent index --full` re-embeds
  everything.
- Files are split into ~1200-character chunks with 200 characters of overlap,
  embedded with bge-small-en-v1.5 (384 dimensions, cosine distance), and stored
  in a sqlite-vec `vec0` table in `/var/lib/piagent/rag.db`.
- The agent calls `search_docs` for questions about your documents and names
  the files it used.

## HTTP API

Base URL: `https://<pi>:8443`. The API is reachable only from private address
ranges: 10/8, 172.16/12, 192.168/16, 169.254/16, fc00::/7, fe80::/10 and
loopback. Every endpoint except `/v1/ping` needs
`Authorization: Bearer <contents of /etc/piagent/token>`. After 10 failed
authentications in 5 minutes, a client IP gets `429` responses.

The certificate is self-signed. Either pin it, or copy it and use it as the CA:

```bash
scp pi@piagent.local:/etc/piagent/tls/cert.pem piagent.pem
TOKEN=$(ssh pi@piagent.local cat /etc/piagent/token)
API=https://piagent.local:8443
curl --cacert piagent.pem -H "Authorization: Bearer $TOKEN" $API/v1/status
```

| Method & path | Body | Result |
|---|---|---|
| `GET /v1/ping` | – | `{"ok": true}` (no auth) |
| `GET /v1/status` | – | model readiness, index stats, device count, TLS fingerprint |
| `POST /v1/chat` | `{"message": "...", "session_id"?: "..."}` | `status`: `done` (with `answer`), `needs_confirmation` (with `pending`), or `error`; `events` lists tool calls and results |
| `POST /v1/sessions/{id}/confirm` | `{"approve": true\|false, "pending_id": "..."}` | continues the run and returns the same shape as `/v1/chat` |
| `GET` / `DELETE /v1/sessions/{id}` | – | transcript / forget the session |
| `POST /v1/search` | `{"query": "...", "k": 4}` | `{"results": [{path, score, text, chunk}]}` |
| `POST /v1/index` | `{"full": false}` | counts of added/updated/removed documents |
| `GET /v1/index` | – | index statistics |
| `GET /v1/devices` | – | enrolled devices, online state, client policy summary |
| `POST /v1/devices` | `{"name": "laptop"}` | `client_config` for the device. **Its secret is shown only once.** |
| `DELETE /v1/devices/{name}` | – | revokes the device |
| `POST /v1/devices/{name}/commands` | `{"command": "...", "timeout": 60, "confirm"?: true, "wait"?: 30}` | `403` if denied, `409 confirmation_required` if destructive and `confirm` is not true; otherwise queued (`202`), or the result if `wait` > 0 |
| `GET /v1/devices/{name}/commands/{id}` | – | status, exit code, stdout/stderr |

Example of a confirmation round-trip:

```bash
R=$(curl -s --cacert piagent.pem -H "Authorization: Bearer $TOKEN" \
     -d '{"message":"remove /var/lib/piagent/work/tmp.txt"}' $API/v1/chat)
echo "$R" | python3 -m json.tool        # "status": "needs_confirmation", "pending": {...}
SID=$(echo "$R" | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')
PID=$(echo "$R" | python3 -c 'import sys,json; print(json.load(sys.stdin)["pending"]["id"])')
curl -s --cacert piagent.pem -H "Authorization: Bearer $TOKEN" \
     -d "{\"approve\": true, \"pending_id\": \"$PID\"}" $API/v1/sessions/$SID/confirm
```

Rotate the token with `sudo piagent token rotate`. The API picks up the new
token immediately.

---

## Companion client for remote devices

`client/piagent_client.py` is a single Python file (3.8+, standard library
only) for **Linux, macOS and Windows**. It lets the agent, or you through the
API, run commands on other machines on your LAN. The client **connects out**
to the Pi by long-polling, so nothing listens on the remote machine. Every
command must pass all of these checks before it runs:

1. **Signature**: HMAC-SHA256 with this device's secret. The Pi signs only
   commands that passed its own safety check, and destructive ones only after
   a human confirmed them.
2. **Freshness**: the command is addressed to this device, not expired, and
   not replayed (seen IDs are remembered).
3. **Local policy**: an allowlist of regexes in the device's own config file,
   which the Pi cannot change. Deny rules and a built-in deny list (sudo,
   formatting, shutdown, recursive deletes, ...) win over the allowlist. Shell
   metacharacters (`; & | < > $ \``) are rejected unless you enable them.
4. Optionally **local approval**: `"confirm_locally": true` asks at the
   device's terminal; with no terminal, the command is rejected.

Commands run **without a shell**, as the user that runs the client, with a
timeout and an output cap. TLS is pinned to the Pi's certificate fingerprint.

### Enroll and install

On the Pi:

```bash
piagent devices add laptop -o laptop.json     # contains the device secret; copy it to the device
```

Copy `client/` and `laptop.json` to the device, then:

| OS | Install | Runs as |
|---|---|---|
| **Linux** | `sudo client/install/install-linux.sh laptop.json` | system user `piagent-client`, systemd unit `piagent-client` with sandboxing |
| **macOS** | `client/install/install-macos.sh laptop.json` | you, LaunchAgent `com.piagent.client`, logs in `~/Library/Logs/piagent-client.log` |
| **Windows** | `powershell -ExecutionPolicy Bypass -File client\install\install-windows.ps1 -EnrollFile .\laptop.json` | you (not elevated), Scheduled Task "PiAgent Client" at logon, logs in `%LOCALAPPDATA%\piagent-client\client.log` |

Or run it by hand:

```bash
python3 piagent_client.py init --from-json laptop.json    # writes config.json with an example allowlist for this OS
python3 piagent_client.py check "df -h"                   # ALLOW/REJECT according to the policy
python3 piagent_client.py test                            # authenticates to the Pi
python3 piagent_client.py run                             # serve commands
```

If `piagent.local` does not resolve, edit `"server"` in the device config to
the Pi's IP, for example `"https://192.168.1.50:8443"`. The fingerprint pin
still protects the connection.

### Policy

`config.json` on the device (Linux example):

```json
{
  "server": "https://piagent.local:8443",
  "device": "laptop",
  "secret": "…",
  "tls_fingerprint": "AB:CD:…",
  "policy": {
    "allow": ["uptime", "df( -h)?", "systemctl status [A-Za-z0-9@._-]+( --no-pager)?",
              "journalctl -u [A-Za-z0-9@._-]+ -n [0-9]{1,4} --no-pager"],
    "deny": [],
    "allow_shell_metachars": false,
    "confirm_locally": false,
    "max_timeout": 120,
    "max_output_bytes": 65536
  }
}
```

Each `allow` entry must match the **whole** command line (Python regex). Keep
patterns tight: `"df( -h)?"` and not `"df.*"`. Restart the client after editing.
The client reports a summary of its allowlist to the Pi, so the agent can see
which commands a device accepts (`list_devices`).

Use from the Pi:

```bash
piagent devices list
piagent devices run laptop -- df -h
piagent ask "is the backup service on my laptop healthy?"    # agent uses remote_exec
```

Revoke a device with `piagent devices remove laptop`. Its credentials stop working immediately.

---

## Security model

- **Network**: nftables drops all inbound traffic except replies, ICMP, DHCP,
  mDNS and TCP 22/8443 from private ranges. The API also checks the source
  address itself. Both llama.cpp servers bind to `127.0.0.1` and their units
  allow only localhost (`IPAddressDeny=any`).
- **Auth**: 256-bit bearer token compared in constant time, with failed attempts
  throttled. Devices have separate per-device keys that cannot use admin
  endpoints. TLS 1.2+ uses a per-device self-signed certificate.
- **Least privilege**: the agent and its shell run as `piagent` (no sudo,
  `NoNewPrivileges`, no capabilities, read-only system directories); llama.cpp
  runs as `llama` with a strict sandbox. The login user is never used by services.
- **Human in the loop**: destructive commands need explicit approval, by
  terminal prompt or the `/confirm` API, and remote commands also need the
  device's own allowlist.
- **Secrets**: generated per device on first boot. The image contains no keys,
  and SSH host keys are regenerated on first boot.
- **Limits**: a determined model can still waste CPU or read files that
  `piagent` can read. Do not put secrets in `/srv/docs`, and review what you approve.

## Services, files and tuning

| Unit | Purpose |
|---|---|
| `llama-server.service` | chat model, `127.0.0.1:8081` |
| `llama-embed.service` | embedding model, `127.0.0.1:8082` |
| `piagent-api.service` | agent + API, `0.0.0.0:8443` |
| `piagent-firstboot.service` | one-time token and certificate |
| `piagent-index.timer` | document re-indexing |
| `nftables.service` | firewall |

Disabled to keep the system minimal: Bluetooth (`dtoverlay=disable-bt`,
`bluetooth`/`hciuart` masked), swap file, udisks2, ModemManager, NFS/rpcbind,
apt/man-db timers. The system boots to `multi-user.target` (no desktop) and
journald is volatile.

| Path | Contents |
|---|---|
| `/etc/piagent/piagent.toml` | agent, RAG, API and device settings |
| `/etc/piagent/llama.env` | model paths, context size (4096), threads |
| `/etc/piagent/token`, `/etc/piagent/tls/` | API token and certificate |
| `/opt/llama.cpp/bin/` | `llama-server`, `llama-cli` |
| `/opt/models/` | GGUF models and `SHA256SUMS` |
| `/opt/piagent/` | Python package and venv |
| `/var/lib/piagent/` | `rag.db`, `devices.db`, agent working directory |
| `/srv/docs/` | your documents |

Memory: the Q4_K_M model is about 2.5 GB, and the 4096-token KV cache adds
about 0.5 GB, so an 8 GB Pi 5 has plenty left. Generation runs at a few tokens
per second. Use active cooling (the official Active Cooler) to avoid thermal
throttling (`vcgencmd get_throttled`).

## Troubleshooting

| Symptom | Check |
|---|---|
| `build.sh`: *qemu-aarch64-static is missing* | `sudo apt install qemu-user-static binfmt-support` |
| `build.sh`: *container exists* | `./build.sh --continue` to resume, `--clean` to start over |
| pi-gen fails in `stage0` on WSL | restart Docker Desktop; make sure the repo is not under `/mnt/c` |
| Build stops at the ABI check | you are not compiling in `debian:bookworm`; keep `BUILDER_IMAGE=debian:bookworm` |
| `piagent status` shows `chat_model_ready: false` | the model is still loading, or `journalctl -u llama-server -b` |
| `cannot read /etc/piagent/token` | log out and in (group membership), or `sudo usermod -aG piagent $USER` |
| API unreachable from the LAN | client on a non-private range? Edit `/etc/nftables.conf` and `allowed_networks` |
| Device shows offline | `piagent_client.py test` on the device; check the fingerprint and the server URL |
| Device rejects everything | `piagent_client.py check "<command>"`; the allow regex must match the whole line |

## Repository layout and tests

```
pi5-agent-image/
├── build.sh                       # the build entry point
├── config/build.conf              # build settings (+ build.local.conf for secrets)
├── scripts/
│   ├── cross-build-llama.sh       # llama.cpp cross-compile + ABI check
│   └── fetch-wheel.py             # pinned PyPI wheel download
├── pi-gen/stage-piagent/          # custom pi-gen stage (packages, install, minimal services)
├── rootfs/                        # files copied into the image (config, units, firstboot, firewall)
├── piagent/piagent/               # the agent: llm, safety, tools, rag, devices, api, cli
├── client/                        # companion client + installers for Linux/macOS/Windows
└── tests/                         # pytest suite (fake llama.cpp server, real HTTPS + client)
```

Run the tests on any machine with Python 3.11+ and openssl:

```bash
python3 -m venv .venv && .venv/bin/pip install pytest sqlite-vec==0.1.9
.venv/bin/python -m pytest -q tests
```

The tests cover the command classifier, chunking and incremental indexing
with real sqlite-vec, the agent's confirm/reject/deny flow, and the HTTPS API
end to end: token auth, throttling, LAN check and confirmation round-trips.
They also run the real companion client, checking signature, replay and
expiry checks, local allowlist and metacharacter rejection, and certificate
pinning.
