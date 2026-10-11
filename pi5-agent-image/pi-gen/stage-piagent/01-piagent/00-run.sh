#!/bin/bash -e
# Install llama.cpp, the models, the PiAgent Python app and its config/units.
#
# Large inputs are prepared by build.sh and bind-mounted read-only at
# $PIAGENT_ARTIFACTS (default /piagent-artifacts) inside the pi-gen container:
#   llama/bin/llama-server, llama/bin/llama-cli   cross-compiled for Cortex-A76
#   models/*.gguf, models/SHA256SUMS              chat + embedding models
#   wheels/*.whl                                  hash-checked Python wheels
#   piagent/piagent/                              the Python package
#   rootfs/                                       config files, systemd units, scripts

A="${PIAGENT_ARTIFACTS:-/piagent-artifacts}"
: "${PIAGENT_CHAT_MODEL_FILE:?set by build.sh}"
: "${PIAGENT_EMBED_MODEL_FILE:?set by build.sh}"
: "${PIAGENT_API_PORT:?set by build.sh}"

for f in "$A/llama/bin/llama-server" "$A/llama/bin/llama-cli" \
	"$A/models/$PIAGENT_CHAT_MODEL_FILE" "$A/models/$PIAGENT_EMBED_MODEL_FILE" "$A/models/SHA256SUMS" \
	"$A/piagent/piagent/__init__.py" "$A/rootfs/etc/piagent/piagent.toml"; do
	if [ ! -e "$f" ]; then
		echo "missing build artifact: $f (run ./build.sh rather than pi-gen directly)" >&2
		exit 1
	fi
done
if ! compgen -G "$A/wheels/*.whl" > /dev/null; then
	echo "missing Python wheels in $A/wheels" >&2
	exit 1
fi

log "Verifying model checksums"
(cd "$A/models" && sha256sum --check --strict SHA256SUMS)

# Service accounts. The agent never runs as root or as the login user.
on_chroot << EOF
getent group piagent > /dev/null || groupadd --system piagent
id -u piagent > /dev/null 2>&1 || useradd --system --gid piagent --groups video \
	--home-dir /var/lib/piagent --no-create-home --shell /usr/sbin/nologin --comment "PiAgent" piagent
getent group llama > /dev/null || groupadd --system llama
id -u llama > /dev/null 2>&1 || useradd --system --gid llama \
	--home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin --comment "llama.cpp server" llama
usermod -aG piagent "${FIRST_USER_NAME}"
EOF

log "Installing llama.cpp ($(cat "$A/llama/COMMIT" 2>/dev/null || echo unknown))"
install -d -m 0755 "${ROOTFS_DIR}/opt/llama.cpp/bin"
install -m 0755 "$A/llama/bin/llama-server" "$A/llama/bin/llama-cli" "${ROOTFS_DIR}/opt/llama.cpp/bin/"
for f in LICENSE.llama.cpp COMMIT; do
	[ -f "$A/llama/$f" ] && install -m 0644 "$A/llama/$f" "${ROOTFS_DIR}/opt/llama.cpp/"
done
ln -sf /opt/llama.cpp/bin/llama-cli "${ROOTFS_DIR}/usr/local/bin/llama-cli"

log "Installing models"
install -d -m 0755 "${ROOTFS_DIR}/opt/models"
install -m 0644 "$A/models/$PIAGENT_CHAT_MODEL_FILE" "$A/models/$PIAGENT_EMBED_MODEL_FILE" \
	"$A/models/SHA256SUMS" "${ROOTFS_DIR}/opt/models/"

log "Installing PiAgent"
install -d -m 0755 "${ROOTFS_DIR}/opt/piagent/lib" "${ROOTFS_DIR}/opt/piagent/wheels"
rm -rf "${ROOTFS_DIR}/opt/piagent/lib/piagent"
rsync -r --exclude '__pycache__' "$A/piagent/piagent" "${ROOTFS_DIR}/opt/piagent/lib/"
install -m 0644 "$A/wheels/"*.whl "${ROOTFS_DIR}/opt/piagent/wheels/"

# Config files, units and scripts: root-owned (no -o/-g), with normalized modes
# so a checkout made under a permissive umask cannot loosen /etc or /usr.
rsync -rlpt --chmod=D0755,F0644 "$A/rootfs/" "${ROOTFS_DIR}/"
sed -i "s|@API_PORT@|${PIAGENT_API_PORT}|g" \
	"${ROOTFS_DIR}/etc/nftables.conf" \
	"${ROOTFS_DIR}/etc/piagent/piagent.toml" \
	"${ROOTFS_DIR}/etc/update-motd.d/60-piagent"
sed -i -e "s|@CHAT_MODEL@|/opt/models/${PIAGENT_CHAT_MODEL_FILE}|" \
	-e "s|@EMBED_MODEL@|/opt/models/${PIAGENT_EMBED_MODEL_FILE}|" \
	"${ROOTFS_DIR}/etc/piagent/llama.env"
chmod 0755 "${ROOTFS_DIR}/usr/local/bin/piagent" "${ROOTFS_DIR}/usr/local/sbin/piagent-firstboot" \
	"${ROOTFS_DIR}/etc/update-motd.d/60-piagent"

on_chroot << EOF
set -e
python3 -m venv /opt/piagent/venv
/opt/piagent/venv/bin/pip install --no-index --no-cache-dir --disable-pip-version-check \
	--find-links /opt/piagent/wheels sqlite-vec
/opt/piagent/venv/bin/python3 -m compileall -q /opt/piagent/lib

# Smoke tests under emulation: catch a wrong-ABI binary or wheel at build time.
/opt/llama.cpp/bin/llama-server --version
/opt/piagent/venv/bin/python3 -c "import sqlite3, sqlite_vec; c = sqlite3.connect(':memory:'); c.enable_load_extension(True); sqlite_vec.load(c); print('sqlite-vec', c.execute('select vec_version()').fetchone()[0], 'on SQLite', sqlite3.sqlite_version)"
PYTHONPATH=/opt/piagent/lib /opt/piagent/venv/bin/python3 -m piagent --version

chown root:piagent /etc/piagent /etc/piagent/piagent.toml
chmod 0750 /etc/piagent
chmod 0640 /etc/piagent/piagent.toml
chmod 0644 /etc/piagent/llama.env
install -d -m 0750 -o piagent -g piagent /var/lib/piagent /var/lib/piagent/work
chown -R "${FIRST_USER_NAME}:piagent" /srv/docs
chmod 2775 /srv/docs
chmod 0664 /srv/docs/README.md

systemctl enable nftables.service llama-server.service llama-embed.service \
	piagent-firstboot.service piagent-api.service piagent-index.timer
EOF
