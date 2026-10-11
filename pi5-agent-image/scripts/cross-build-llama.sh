#!/usr/bin/env bash
# Cross-compile llama.cpp for the Raspberry Pi 5 (Cortex-A76, aarch64).
#
# Normally run by build.sh inside a debian:bookworm container so the binaries
# link against the same glibc/libstdc++ as Raspberry Pi OS Bookworm:
#
#   cross-build-llama.sh --install-deps /src /out
#
# It also works on any host that already has an aarch64-linux-gnu cross
# toolchain and cmake installed (omit --install-deps).
set -euo pipefail

INSTALL_DEPS=0
if [ "${1:-}" = "--install-deps" ]; then
	INSTALL_DEPS=1
	shift
fi

SRC_DIR="${1:?usage: $0 [--install-deps] SRC_DIR OUT_DIR}"
OUT_DIR="${2:?usage: $0 [--install-deps] SRC_DIR OUT_DIR}"
# Cortex-A76: ARMv8.2-A with dot-product and half-precision arithmetic.
ARM_ARCH="${LLAMA_ARM_ARCH:-armv8.2-a+dotprod+fp16}"
JOBS="${JOBS:-$(nproc)}"
BUILD_DIR="${BUILD_DIR:-/tmp/llama-build}"
CROSS="${CROSS_PREFIX:-aarch64-linux-gnu-}"
CC_BIN="${CROSS_CC:-${CROSS}gcc}"
CXX_BIN="${CROSS_CXX:-${CROSS}g++}"
# Raspberry Pi OS Bookworm ships glibc 2.36 and libstdc++ from GCC 12.
MAX_GLIBC="${MAX_GLIBC:-2.36}"
MAX_GLIBCXX="${MAX_GLIBCXX:-3.4.30}"

if [ "$INSTALL_DEPS" = "1" ]; then
	export DEBIAN_FRONTEND=noninteractive
	apt-get update
	apt-get install -y --no-install-recommends \
		crossbuild-essential-arm64 cmake ninja-build git ca-certificates file
fi

# The source tree is usually owned by another uid (bind mount); let git read
# it so llama.cpp can embed its build number and commit.
git config --global --add safe.directory "$SRC_DIR" 2>/dev/null || true

rm -rf "$BUILD_DIR"
cmake -S "$SRC_DIR" -B "$BUILD_DIR" -G Ninja \
	-DCMAKE_BUILD_TYPE=Release \
	-DCMAKE_SYSTEM_NAME=Linux \
	-DCMAKE_SYSTEM_PROCESSOR=aarch64 \
	-DCMAKE_C_COMPILER="$CC_BIN" \
	-DCMAKE_CXX_COMPILER="$CXX_BIN" \
	-DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER \
	-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY \
	-DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY \
	-DBUILD_SHARED_LIBS=OFF \
	-DGGML_NATIVE=OFF \
	-DGGML_CPU_ARM_ARCH="$ARM_ARCH" \
	-DGGML_OPENMP=ON \
	-DGGML_CCACHE=OFF \
	-DLLAMA_OPENSSL=OFF \
	-DLLAMA_BUILD_UI=OFF \
	-DLLAMA_USE_PREBUILT_UI=OFF \
	-DLLAMA_BUILD_TESTS=OFF \
	-DLLAMA_BUILD_EXAMPLES=OFF \
	-DLLAMA_BUILD_APP=OFF \
	-DLLAMA_BUILD_TOOLS=ON \
	-DLLAMA_BUILD_SERVER=ON

cmake --build "$BUILD_DIR" --parallel "$JOBS" --target llama-server llama-cli

# Refuse binaries that need a newer C/C++ runtime than the target OS has
# (happens when cross-compiling with a non-Bookworm toolchain).
newest() {
	"${CROSS}objdump" -T "$BUILD_DIR/bin/llama-server" | grep -o "$1_[0-9.]*" | sed "s/^$1_//" | sort -uV | tail -n1
}
check_abi() {
	local have
	have="$(newest "$1")"
	if [ -n "$have" ] && [ "$(printf '%s\n%s\n' "$have" "$2" | sort -V | tail -n1)" != "$2" ]; then
		echo "ERROR: llama-server needs $1 $have but the target only has $2." >&2
		echo "       Build inside debian:bookworm (build.sh does this) or set MAX_$1." >&2
		exit 1
	fi
}
check_abi GLIBC "$MAX_GLIBC"
check_abi GLIBCXX "$MAX_GLIBCXX"

mkdir -p "$OUT_DIR/bin"
install -m 0755 "$BUILD_DIR/bin/llama-server" "$BUILD_DIR/bin/llama-cli" "$OUT_DIR/bin/"
"${CROSS}strip" "$OUT_DIR/bin/llama-server" "$OUT_DIR/bin/llama-cli"
cp "$SRC_DIR/LICENSE" "$OUT_DIR/LICENSE.llama.cpp"
git -C "$SRC_DIR" rev-parse HEAD > "$OUT_DIR/COMMIT" 2>/dev/null || echo unknown > "$OUT_DIR/COMMIT"

if command -v file >/dev/null 2>&1; then
	file "$OUT_DIR/bin/llama-server"
fi
echo "llama.cpp aarch64 binaries written to $OUT_DIR/bin"
