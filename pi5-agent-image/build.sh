#!/usr/bin/env bash
# Build a flashable Raspberry Pi 5 image: Raspberry Pi OS Lite (Bookworm,
# arm64) + llama.cpp + Phi-4-mini-instruct + PiAgent, using pi-gen in Docker.
#
#   ./build.sh                 full build (downloads are cached in ./cache)
#   ./build.sh --continue      resume a failed pi-gen run in its container
#                              (SKIP_STAGES="stage0 stage1 stage2" skips finished stages)
#   ./build.sh --clean         remove the pi-gen container and work files first
#   ./build.sh --fetch-only    download/compile inputs, stop before pi-gen
#   ./build.sh -c FILE         use another config file (default config/build.conf)
#
# Works on Linux and in WSL2 (Docker Desktop with WSL integration, or Docker
# Engine installed inside the WSL distribution). See README.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$ROOT/config/build.conf"
CONTINUE=0
CLEAN=0
FETCH_ONLY=0

while [ $# -gt 0 ]; do
	case "$1" in
	-c | --config) CONFIG="$(realpath "$2")"; shift 2 ;;
	--continue) CONTINUE=1; shift ;;
	--clean) CLEAN=1; shift ;;
	--fetch-only) FETCH_ONLY=1; shift ;;
	-h | --help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
	*) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
	esac
done

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Networks where outbound traffic must go through an HTTPS proxy (optionally
# with its own CA): see BUILD_HTTPS_PROXY in config/build.conf.
proxy_mode() { [ -n "${BUILD_HTTPS_PROXY:-}" ]; }

# docker run options that let a build container use that proxy.
proxy_docker_opts() {
	proxy_mode || return 0
	printf -- '--network host -e BUILD_HTTPS_PROXY=%s -e https_proxy=%s -e HTTPS_PROXY=%s ' \
		"$BUILD_HTTPS_PROXY" "$BUILD_HTTPS_PROXY" "$BUILD_HTTPS_PROXY"
	if [ -n "${BUILD_CA_FILE:-}" ]; then
		printf -- '-v %s:/etc/build-ca.crt:ro ' "$BUILD_CA_FILE"
	fi
}

[ -f "$CONFIG" ] || die "config file $CONFIG not found"
# shellcheck source=config/build.conf
source "$CONFIG"
LOCAL_CONFIG="$(dirname "$CONFIG")/build.local.conf"
if [ -f "$LOCAL_CONFIG" ]; then
	# shellcheck disable=SC1090
	source "$LOCAL_CONFIG"
fi

CACHE="${CACHE_DIR:-$ROOT/cache}"
ART="$CACHE/artifacts"
WORK="$ROOT/.build"
PIGEN="$WORK/pi-gen"
DEPLOY="$ROOT/deploy"
DOCKER="${DOCKER:-docker}"

# ---------------------------------------------------------------------------
preflight() {
	say "Checking the build host"
	case "$ROOT$CACHE" in
	*" "*) die "the build path must not contain spaces (pi-gen/debootstrap limitation): $ROOT" ;;
	esac
	if grep -qi microsoft /proc/version 2>/dev/null && [[ "$ROOT" == /mnt/* ]]; then
		die "you are building on a Windows drive ($ROOT). Clone into the WSL file system (e.g. ~/pi5-agent-image); /mnt/c is slow and loses Linux permissions."
	fi
	local tool
	for tool in git curl sha256sum python3 realpath; do
		command -v "$tool" > /dev/null || die "missing tool: $tool (sudo apt install git curl coreutils python3)"
	done
	command -v "$DOCKER" > /dev/null || die "docker not found. Install Docker Desktop (WSL integration) or Docker Engine."
	if ! $DOCKER info > /dev/null 2>&1; then
		if sudo -n true 2> /dev/null && sudo "$DOCKER" info > /dev/null 2>&1; then
			DOCKER="sudo $DOCKER"
		else
			die "cannot talk to the Docker daemon. Start Docker (Desktop) or add yourself to the docker group."
		fi
	fi
	if [ "$(uname -m)" != "aarch64" ]; then
		command -v qemu-aarch64-static > /dev/null ||
			die "qemu-aarch64-static is missing; install it with: sudo apt install qemu-user-static binfmt-support"
		if [ ! -e /proc/sys/fs/binfmt_misc/register ]; then
			sudo mount binfmt_misc -t binfmt_misc /proc/sys/fs/binfmt_misc 2> /dev/null ||
				warn "binfmt_misc is not mounted; pi-gen will try to mount it"
		fi
	fi
	local avail
	avail="$(df -P -BG "$ROOT" | awk 'NR==2 {gsub("G","",$4); print $4}')"
	if [ "${avail:-0}" -lt 35 ]; then
		warn "only ${avail}G free under $ROOT; a full build needs about 35G (images, rootfs, Docker layers)"
	fi
	if [ "${PUBKEY_ONLY_SSH:-0}" = "1" ] && [ -z "${PUBKEY_SSH_FIRST_USER:-}" ]; then
		die "PUBKEY_ONLY_SSH=1 needs PUBKEY_SSH_FIRST_USER"
	fi
	if [ -n "${WIFI_SSID:-}" ] && [ -z "${WPA_COUNTRY:-}" ]; then
		die "WIFI_SSID is set, so WPA_COUNTRY (e.g. US, GB, DE) must be set too"
	fi
	if [ -n "${WIFI_PSK:-}" ] && { [ "${#WIFI_PSK}" -lt 8 ] || [ "${#WIFI_PSK}" -gt 63 ]; }; then
		die "WIFI_PSK must be 8-63 characters"
	fi
	if proxy_mode; then
		if [ -n "${BUILD_CA_FILE:-}" ]; then
			BUILD_CA_FILE="$(realpath "$BUILD_CA_FILE")"
			[ -f "$BUILD_CA_FILE" ] || die "BUILD_CA_FILE $BUILD_CA_FILE not found"
			case "$BUILD_CA_FILE" in *" "*) die "the BUILD_CA_FILE path must not contain spaces" ;; esac
		fi
		echo "Build containers will use the HTTPS proxy $BUILD_HTTPS_PROXY${BUILD_CA_FILE:+ and trust $BUILD_CA_FILE}"
	fi
	mkdir -p "$ART/models" "$ART/wheels" "$CACHE/src" "$WORK" "$DEPLOY"
}

# Files written from inside containers can be root-owned.
remove_tree() {
	if [ -e "$1" ]; then
		rm -rf "$1" 2> /dev/null || sudo rm -rf "$1"
	fi
}

# Clone (or reuse) a git tag and make sure it is the pinned commit.
fetch_git() {
	local repo="$1" ref="$2" commit="$3" dest="$4"
	if [ -d "$dest/.git" ] && [ "$(git -C "$dest" rev-parse HEAD)" = "$commit" ]; then
		echo "$dest: $ref ($commit) already present"
		return
	fi
	remove_tree "$dest"
	git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$ref" "$repo" "$dest"
	local head
	head="$(git -C "$dest" rev-parse HEAD)"
	if [ -n "$commit" ] && [ "$head" != "$commit" ]; then
		die "$repo $ref is at $head, expected pinned commit $commit (tag moved?)"
	fi
	echo "$dest: $ref ($head)"
}

fetch_sources() {
	say "Fetching pi-gen $PIGEN_REF and llama.cpp $LLAMA_CPP_REF"
	if [ "$CLEAN" = "1" ]; then
		$DOCKER rm -v "$PIGEN_CONTAINER" > /dev/null 2>&1 || true
		remove_tree "$PIGEN"
	fi
	fetch_git "$PIGEN_REPO" "$PIGEN_REF" "$PIGEN_COMMIT" "$PIGEN"
	fetch_git "$LLAMA_CPP_REPO" "$LLAMA_CPP_REF" "$LLAMA_CPP_COMMIT" "$CACHE/src/llama.cpp-$LLAMA_CPP_REF"
}

docker_proxy_env() {
	local v
	for v in http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY; do
		[ -n "${!v:-}" ] && printf -- '-e %s ' "$v"
	done
	return 0
}

build_llama() {
	local out="$ART/llama" id="$LLAMA_CPP_COMMIT $LLAMA_ARM_ARCH $BUILDER_IMAGE"
	if [ -x "$out/bin/llama-server" ] && [ "$(cat "$out/BUILD_ID" 2> /dev/null)" = "$id" ]; then
		say "llama.cpp $LLAMA_CPP_REF already built for the Pi 5 (cached)"
		return
	fi
	say "Cross-compiling llama.cpp $LLAMA_CPP_REF for Cortex-A76 in $BUILDER_IMAGE (a few minutes)"
	remove_tree "$out"
	mkdir -p "$out"
	# shellcheck disable=SC2046,SC2086
	$DOCKER run --rm $(docker_proxy_env) $(proxy_docker_opts) ${BUILDER_DOCKER_OPTS:-} \
		-e LLAMA_ARM_ARCH="$LLAMA_ARM_ARCH" \
		-v "$CACHE/src/llama.cpp-$LLAMA_CPP_REF:/src:ro" \
		-v "$ROOT/scripts:/scripts:ro" \
		-v "$out:/out" \
		"$BUILDER_IMAGE" \
		bash -c "/scripts/cross-build-llama.sh --install-deps /src /out && chown -R $(id -u):$(id -g) /out"
	echo "$id" > "$out/BUILD_ID"
}

# download URL FILE SHA256: fetch into the model cache and verify.
download_model() {
	local url="$1" file="$2" want="${3,,}" dest="$ART/models/$2"
	if [ -f "$dest" ]; then
		echo "$file: cached"
	else
		echo "$file: downloading $url"
		local auth=()
		[ -n "${HF_TOKEN:-}" ] && auth=(-H "Authorization: Bearer $HF_TOKEN")
		curl -fL --retry 5 --retry-delay 5 -C - "${auth[@]}" -o "$dest.part" "$url" ||
			die "download failed: $url (if you are offline, put $file into $ART/models/ yourself)"
		mv "$dest.part" "$dest"
	fi
	local got
	got="$(sha256sum "$dest" | cut -d' ' -f1)"
	if [ -n "$want" ] && [ "$got" != "$want" ]; then
		mv "$dest" "$dest.bad"
		die "$file: sha256 $got does not match the pinned $want (moved to $dest.bad)"
	fi
	[ -z "$want" ] && warn "$file has no pinned hash; recorded $got (set it in config/build.conf to pin)"
	echo "$got  $file" >> "$ART/models/SHA256SUMS.new"
}

fetch_models() {
	say "Fetching models (Phi-4-mini-instruct Q4_K_M is about 2.5 GB)"
	rm -f "$ART/models/SHA256SUMS.new"
	download_model "$CHAT_MODEL_URL" "$CHAT_MODEL_FILE" "${CHAT_MODEL_SHA256:-}"
	download_model "$EMBED_MODEL_URL" "$EMBED_MODEL_FILE" "${EMBED_MODEL_SHA256:-}"
	mv "$ART/models/SHA256SUMS.new" "$ART/models/SHA256SUMS"
	local magic
	for f in "$CHAT_MODEL_FILE" "$EMBED_MODEL_FILE"; do
		magic="$(head -c 4 "$ART/models/$f")"
		[ "$magic" = "GGUF" ] || die "$f is not a GGUF file (got an HTML error page?)"
	done
}

fetch_wheels() {
	say "Fetching Python wheels for aarch64"
	python3 "$ROOT/scripts/fetch-wheel.py" --package sqlite-vec --version "$SQLITE_VEC_VERSION" \
		--sha256 "${SQLITE_VEC_SHA256:-}" --dest "$ART/wheels"
}

stage_app() {
	say "Staging PiAgent files"
	rm -rf "$ART/piagent" "$ART/rootfs"
	mkdir -p "$ART/piagent"
	cp -r "$ROOT/piagent/piagent" "$ART/piagent/"
	find "$ART/piagent" -name '__pycache__' -prune -exec rm -rf {} +
	cp -r "$ROOT/rootfs" "$ART/rootfs"
}

# Shell-quote a config line for pi-gen's config file.
kv() { printf '%s=%q\n' "$1" "$2"; }
xkv() { printf 'export %s=%q\n' "$1" "$2"; }

# pi-gen's export stage needs the build kernel to parse MBR partitions on loop
# devices and to mount vfat. Some VM/container kernels have neither, so add
# fallbacks: register partitions with partx and, if vfat cannot be mounted,
# write the boot partition with mtools. Both are no-ops on ordinary kernels.
patch_pigen_kernel_compat() {
	python3 - "$PIGEN" << 'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])

MTOOLS_STEP = r'''if [ -f "${STAGE_WORK_DIR}/boot-via-mtools" ]; then
	# build.sh: copy /boot/firmware into the FAT partition without mounting it
	BOOT_DEV="${ROOT_DEV%2}1"
	export MTOOLS_SKIP_CHECK=1
	mcopy -s -p -m -Q -i "$BOOT_DEV" "${ROOTFS_DIR}/boot/firmware/"* ::/
	for f in config.txt cmdline.txt kernel_2712.img initramfs_2712 issue.txt; do
		mdir -b -i "$BOOT_DEV" "::/$f" > /dev/null
	done
	echo "boot partition written with mtools: $(mdir -/ -b -i "$BOOT_DEV" ::/ | wc -l) entries"
	rm -rf "${ROOTFS_DIR}/boot/firmware/"*
	rm -f "${STAGE_WORK_DIR}/boot-via-mtools"
fi

'''


def patch(rel, old, new):
    path = root / rel
    text = path.read_text()
    if text.count(old) != 1:
        sys.exit(f"build.sh: cannot patch pi-gen {rel} (layout changed?)")
    path.write_text(text.replace(old, new))


patch("Dockerfile", " dosfstools ", " dosfstools mtools ")
patch(
    "export-image/prerun.sh",
    'ensure_loopdev_partitions "$LOOP_DEV"\n',
    "# build.sh: kernels without MBR partition parsing create no loopNpM nodes\n"
    'partx -a "$LOOP_DEV" 2> /dev/null || true\n'
    'ensure_loopdev_partitions "$LOOP_DEV"\n',
)
patch(
    "export-image/prerun.sh",
    'mount -v "$BOOT_DEV" "${ROOTFS_DIR}/boot/firmware" -t vfat\n',
    'if ! mount -v "$BOOT_DEV" "${ROOTFS_DIR}/boot/firmware" -t vfat; then\n'
    "\t# build.sh: kernel without vfat; 05-finalise writes the files with mtools\n"
    '\techo "vfat cannot be mounted here; the boot partition will be written with mtools"\n'
    '\ttouch "${STAGE_WORK_DIR}/boot-via-mtools"\n'
    "fi\n",
)
patch(
    "export-image/05-finalise/01-run.sh",
    'unmount "${ROOTFS_DIR}"\nzerofree',
    MTOOLS_STEP + 'unmount "${ROOTFS_DIR}"\nzerofree',
)
PY
}

# Make pi-gen fetch everything over HTTPS through BUILD_HTTPS_PROXY. Applied to
# the checkout in .build/ only; the finished image keeps the stock apt sources.
patch_pigen_for_proxy() {
	say "Adapting pi-gen to fetch packages over HTTPS via $BUILD_HTTPS_PROXY"
	local snip_after snip_before line
	if [ -n "${BUILD_CA_FILE:-}" ]; then
		cp "$BUILD_CA_FILE" "$PIGEN/build-ca.crt"
	fi

	# 1. The pi-gen builder image: apt over HTTPS via the proxy, and the CA
	#    trusted system-wide (debootstrap's wget, curl).
	snip_after="$(
		[ -n "${BUILD_CA_FILE:-}" ] && echo "COPY build-ca.crt /etc/build-ca.crt"
		cat << 'EOF'
ARG BUILD_HTTPS_PROXY
RUN sed -i 's#http://deb.debian.org#https://deb.debian.org#g' /etc/apt/sources.list.d/debian.sources \
    && echo "Acquire::https::Proxy \"${BUILD_HTTPS_PROXY}\";" > /etc/apt/apt.conf.d/99build-proxy \
    && if [ -f /etc/build-ca.crt ]; then echo 'Acquire::https::CAInfo "/etc/build-ca.crt";' >> /etc/apt/apt.conf.d/99build-proxy; fi
EOF
	)"
	snip_before='RUN if [ -f /etc/build-ca.crt ]; then cp /etc/build-ca.crt /usr/local/share/ca-certificates/build-ca.crt && update-ca-certificates; fi'
	while IFS= read -r line; do
		case "$line" in "COPY . /pi-gen/"*) printf '%s\n' "$snip_before" ;; esac
		printf '%s\n' "$line"
		case "$line" in "ENV DEBIAN_FRONTEND"*) printf '%s\n' "$snip_after" ;; esac
	done < "$PIGEN/Dockerfile" > "$PIGEN/Dockerfile.build-proxy"
	# shellcheck disable=SC2016  # literal ${...}: edits a line of build-docker.sh
	sed -i 's#\${DOCKER} build --build-arg#${DOCKER} build ${PIGEN_DOCKER_BUILD_OPTS:-} --build-arg#' "$PIGEN/build-docker.sh"

	# 2. Debian and Raspberry Pi archives over HTTPS while building.
	sed -i 's#http://deb.debian.org#https://deb.debian.org#g' \
		"$PIGEN/stage0/prerun.sh" "$PIGEN/stage0/00-configure-apt/files/sources.list"
	sed -i 's#http://archive.raspberrypi.com#https://archive.raspberrypi.com#g' \
		"$PIGEN/stage0/00-configure-apt/files/raspi.list"

	# 3. apt inside the image rootfs uses the proxy (runs before 00-configure-apt).
	mkdir -p "$PIGEN/stage0/00-build-proxy"
	{
		echo '#!/bin/bash -e'
		echo '# Added by build.sh (BUILD_HTTPS_PROXY); removed again in export-image/02-set-sources.'
		printf 'PROXY=%q\n' "$BUILD_HTTPS_PROXY"
		cat << 'EOF'
{
	echo "Acquire::https::Proxy \"${PROXY}\";"
	if [ -f /etc/build-ca.crt ]; then echo 'Acquire::https::CAInfo "/etc/apt/build-ca.crt";'; fi
} > "${ROOTFS_DIR}/etc/apt/apt.conf.d/99build-proxy"
if [ -f /etc/build-ca.crt ]; then install -m 644 /etc/build-ca.crt "${ROOTFS_DIR}/etc/apt/build-ca.crt"; fi
EOF
	} > "$PIGEN/stage0/00-build-proxy/00-run.sh"
	chmod +x "$PIGEN/stage0/00-build-proxy/00-run.sh"

	# 4. After the last apt run, restore the stock apt configuration in the image.
	cat >> "$PIGEN/export-image/02-set-sources/01-run.sh" << 'EOF'

# Added by build.sh (BUILD_HTTPS_PROXY): ship the stock apt configuration.
rm -f "${ROOTFS_DIR}/etc/apt/apt.conf.d/99build-proxy" "${ROOTFS_DIR}/etc/apt/build-ca.crt"
sed -i -e 's#https://deb.debian.org#http://deb.debian.org#g' \
	-e 's#https://archive.raspberrypi.com#http://archive.raspberrypi.com#g' \
	"${ROOTFS_DIR}/etc/apt/sources.list" "${ROOTFS_DIR}/etc/apt/sources.list.d/raspi.list"
EOF

	# shellcheck disable=SC2015  # die if any check fails
	grep -q 99build-proxy "$PIGEN/Dockerfile.build-proxy" &&
		grep -q update-ca-certificates "$PIGEN/Dockerfile.build-proxy" &&
		grep -q PIGEN_DOCKER_BUILD_OPTS "$PIGEN/build-docker.sh" &&
		grep -q "https://deb.debian.org" "$PIGEN/stage0/prerun.sh" ||
		die "pi-gen's layout changed; BUILD_HTTPS_PROXY could not be applied"
}

prepare_pigen() {
	say "Preparing pi-gen"
	# Undo changes from an earlier run, then apply what this configuration needs.
	git -C "$PIGEN" checkout -q -- .
	rm -rf "$PIGEN/stage0/00-build-proxy" "$PIGEN/Dockerfile.build-proxy" "$PIGEN/build-ca.crt"
	patch_pigen_kernel_compat
	if proxy_mode; then
		patch_pigen_for_proxy
	fi
	rsync -a --delete "$ROOT/pi-gen/stage-piagent/" "$PIGEN/stage-piagent/" 2> /dev/null ||
		{ rm -rf "$PIGEN/stage-piagent" && cp -r "$ROOT/pi-gen/stage-piagent" "$PIGEN/stage-piagent"; }
	# Export only the final image, not the intermediate Lite image.
	touch "$PIGEN/stage2/SKIP_IMAGES"
	# SKIP_STAGES="stage0 stage1 ..." with --continue resumes after stages that
	# already completed in the existing container (e.g. after a failed export).
	local st
	for st in stage0 stage1 stage2 stage-piagent; do
		rm -f "$PIGEN/$st/SKIP"
	done
	for st in ${SKIP_STAGES:-}; do
		[ -d "$PIGEN/$st" ] || die "SKIP_STAGES: unknown stage $st"
		touch "$PIGEN/$st/SKIP"
		echo "skipping $st (SKIP_STAGES)"
	done

	if [ -z "${FIRST_USER_PASS:-}" ]; then
		local pwfile="$DEPLOY/${IMG_NAME}-${FIRST_USER_NAME}-password.txt"
		if [ -s "$pwfile" ]; then
			FIRST_USER_PASS="$(cat "$pwfile")"
		else
			FIRST_USER_PASS="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
			(umask 077 && echo "$FIRST_USER_PASS" > "$pwfile")
			warn "generated a password for '$FIRST_USER_NAME': $FIRST_USER_PASS (saved in $pwfile)"
		fi
	fi

	{
		echo "# Generated by build.sh from $(basename "$CONFIG"); edit that instead."
		kv IMG_NAME "$IMG_NAME"
		kv RELEASE bookworm
		kv DEPLOY_COMPRESSION "$DEPLOY_COMPRESSION"
		kv TARGET_HOSTNAME "$TARGET_HOSTNAME"
		kv FIRST_USER_NAME "$FIRST_USER_NAME"
		kv FIRST_USER_PASS "$FIRST_USER_PASS"
		kv DISABLE_FIRST_BOOT_USER_RENAME 1
		kv ENABLE_SSH 1
		[ -n "${PUBKEY_SSH_FIRST_USER:-}" ] && kv PUBKEY_SSH_FIRST_USER "$PUBKEY_SSH_FIRST_USER"
		kv PUBKEY_ONLY_SSH "${PUBKEY_ONLY_SSH:-0}"
		kv LOCALE_DEFAULT "$LOCALE_DEFAULT"
		kv KEYBOARD_KEYMAP "$KEYBOARD_KEYMAP"
		kv KEYBOARD_LAYOUT "$KEYBOARD_LAYOUT"
		kv TIMEZONE_DEFAULT "$TIMEZONE_DEFAULT"
		[ -n "${WPA_COUNTRY:-}" ] && kv WPA_COUNTRY "$WPA_COUNTRY"
		kv STAGE_LIST "stage0 stage1 stage2 stage-piagent"
		xkv PIAGENT_ARTIFACTS /piagent-artifacts
		xkv PIAGENT_API_PORT "$PIAGENT_API_PORT"
		xkv PIAGENT_CHAT_MODEL_FILE "$CHAT_MODEL_FILE"
		xkv PIAGENT_EMBED_MODEL_FILE "$EMBED_MODEL_FILE"
		xkv PIAGENT_DISABLE_BT "${DISABLE_BLUETOOTH:-1}"
		xkv PIAGENT_WIFI_SSID "${WIFI_SSID:-}"
		xkv PIAGENT_WIFI_PSK "${WIFI_PSK:-}"
	} > "$PIGEN/config"
	chmod 600 "$PIGEN/config"
}

run_pigen() {
	local exists
	exists="$($DOCKER ps -a --filter "name=^${PIGEN_CONTAINER}$" -q)"
	if [ -n "$exists" ] && [ "$CONTINUE" != "1" ]; then
		if [ "$CLEAN" = "1" ]; then
			$DOCKER rm -v "$PIGEN_CONTAINER" > /dev/null
		else
			die "a previous pi-gen container ($PIGEN_CONTAINER) exists. Use --continue to resume it or --clean to start over."
		fi
	fi
	say "Running pi-gen (Raspberry Pi OS Lite Bookworm arm64 + PiAgent); this takes 30-90 minutes"
	(
		cd "$PIGEN"
		export CONTAINER_NAME="$PIGEN_CONTAINER"
		export CONTINUE PRESERVE_CONTAINER=0
		if proxy_mode; then
			export PIGEN_DOCKER_BUILD_OPTS="--network host -f $PIGEN/Dockerfile.build-proxy --build-arg BUILD_HTTPS_PROXY=$BUILD_HTTPS_PROXY"
			PIGEN_DOCKER_OPTS="$(proxy_docker_opts)${PIGEN_DOCKER_OPTS:-}"
		fi
		export PIGEN_DOCKER_OPTS="--volume $ART:/piagent-artifacts:ro ${PIGEN_DOCKER_OPTS:-}"
		[[ "$DOCKER" == sudo* ]] && export DOCKER=docker
		./build-docker.sh
	)
}

collect() {
	say "Collecting the image"
	local found="" f
	for f in "$PIGEN"/deploy/*-piagent.img "$PIGEN"/deploy/*-piagent.img.xz \
		"$PIGEN"/deploy/*-piagent.img.gz "$PIGEN"/deploy/*-piagent.zip; do
		[ -f "$f" ] && found="$f"
	done
	[ -n "$found" ] || die "pi-gen finished but no *-piagent image is in $PIGEN/deploy"
	local name
	name="$(basename "$found")"
	mv -f "$found" "$DEPLOY/$name"
	for f in "$PIGEN"/deploy/*-piagent.info "$PIGEN"/deploy/*-piagent.bmap "$PIGEN"/deploy/build*.log; do
		[ -f "$f" ] && mv -f "$f" "$DEPLOY/"
	done
	(cd "$DEPLOY" && sha256sum "$name" > "$name.sha256")
	say "Done"
	ls -lh "$DEPLOY/$name"
	cat "$DEPLOY/$name.sha256"
	echo
	local flash dd_out="sudo dd of=/dev/sdX bs=4M conv=fsync status=progress"
	case "$name" in
	*.img.xz) flash="xzcat $DEPLOY/$name | $dd_out" ;;
	*.img.gz) flash="zcat $DEPLOY/$name | $dd_out" ;;
	*.zip) flash="unzip -p $DEPLOY/$name | $dd_out" ;;
	*) flash="sudo dd if=$DEPLOY/$name of=/dev/sdX bs=4M conv=fsync status=progress" ;;
	esac
	echo "Flash it with Raspberry Pi Imager ('Use custom'), or on Linux (check the device with lsblk first):"
	echo "  $flash"
	echo "Login: $FIRST_USER_NAME@$TARGET_HOSTNAME.local (password: see config or $DEPLOY/*-password.txt)"
}

preflight
fetch_sources
build_llama
fetch_models
fetch_wheels
stage_app
if [ "$FETCH_ONLY" = "1" ]; then
	say "Inputs are ready in $ART (--fetch-only)"
	exit 0
fi
prepare_pigen
run_pigen
collect
