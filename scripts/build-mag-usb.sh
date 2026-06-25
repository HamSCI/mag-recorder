#!/bin/bash
# build-mag-usb.sh — idempotent from-source build of mag-usb
#
# Usage: sudo ./scripts/build-mag-usb.sh [--force] [--no-apt]
#
# Clones HamSCI/mag-usb (sigmond-integration branch — carries our
# fixes that aren't in wittend/mag-usb upstream yet) into a scratch dir,
# builds the C executable with cmake, installs it to
# <repo>/bin/mag-usb, and writes <repo>/bin/mag-usb.provenance.
#
# Skips work that is already up to date.
#
# Honors these env vars:
#   MAG_RECORDER_PREFIX     install prefix         (default: /opt/git/sigmond/mag-recorder)
#   MAG_RECORDER_BUILD_DIR  scratch build dir      (default: /var/cache/mag-recorder/build)
#   MAG_USB_URL             override remote        (default: https://github.com/HamSCI/mag-usb.git)
#   MAG_USB_REF             git ref                (default: sigmond-integration)
#
# After a successful run, ${PREFIX}/bin/mag-usb is on disk, reports
# its version cleanly, and a YAML provenance sidecar is alongside it.
#
# Convention: see sigmond/docs/native-binaries.md.

set -euo pipefail

PREFIX="${MAG_RECORDER_PREFIX:-/opt/git/sigmond/mag-recorder}"
BUILD_DIR="${MAG_RECORDER_BUILD_DIR:-/var/cache/mag-recorder/build}"
MAG_USB_URL="${MAG_USB_URL:-https://github.com/HamSCI/mag-usb.git}"
MAG_USB_REF="${MAG_USB_REF:-sigmond-integration}"

APT_DEPS=(
    # build-essential brings g++, needed because ENABLE_WEBSOCKET=ON
    # compiles src/ws_bridge.cpp via the vendored mengrao-websocket header.
    build-essential cmake pkg-config git
)

ui_info()  { echo "[INFO]  $*"; }
ui_warn()  { echo "[WARN]  $*" >&2; }
ui_error() { echo "[ERROR] $*" >&2; }

FORCE=false
SKIP_APT=false
for arg in "$@"; do
    case "$arg" in
        --force)  FORCE=true ;;
        --no-apt) SKIP_APT=true ;;
        *)        ui_warn "Ignoring unknown arg: $arg" ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    ui_error "Must run as root (sudo)"
    exit 1
fi

ensure_apt_deps() {
    if $SKIP_APT; then
        ui_info "Skipping apt deps (--no-apt)"
        return
    fi
    local missing=()
    for pkg in "${APT_DEPS[@]}"; do
        if ! dpkg -s "$pkg" >/dev/null 2>&1; then
            missing+=("$pkg")
        fi
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        ui_info "All apt build deps already present"
        return
    fi
    ui_info "Installing apt deps: ${missing[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
}

clone_or_update() {
    local url="$1" ref="$2" dest="$3"
    if [[ ! -d "$dest/.git" ]]; then
        ui_info "Cloning $url -> $dest"
        git clone "$url" "$dest"
    else
        ui_info "Fetching $dest"
        git -C "$dest" fetch --tags --prune origin
    fi
    ui_info "Checking out $ref in $dest"
    git -C "$dest" checkout --quiet "$ref"
    if git -C "$dest" symbolic-ref -q HEAD >/dev/null; then
        git -C "$dest" pull --ff-only --quiet
    fi
}

build_mag_usb() {
    local src="$1"
    local build="$src/build"
    local stamp="$build/.installed-rev"
    local current_rev
    current_rev=$(git -C "$src" rev-parse HEAD)
    local stamp_content="${current_rev}@${PREFIX}"

    if ! $FORCE && [[ -f "$stamp" ]] && [[ "$(cat "$stamp")" == "$stamp_content" ]]; then
        ui_info "mag-usb @ $current_rev already installed at $PREFIX; skipping (use --force to rebuild)"
        return
    fi

    ui_info "Configuring mag-usb (rev $current_rev)"
    rm -rf "$build"
    # ENABLE_WEBSOCKET=ON matches the upstream default and lets operators
    # turn on mag-usb's optional WebSocket output via mag-recorder config.
    # Pulls in a C++11 toolchain (g++) at build time but adds no runtime
    # apt deps (mengrao-websocket is header-only and vendored).
    # BUILD_TESTING=OFF skips the in-tree test executables; sigmond
    # doesn't need them in the shipped artifact.
    cmake -S "$src" -B "$build" \
        -DCMAKE_BUILD_TYPE=Release \
        -DENABLE_WEBSOCKET=ON \
        -DBUILD_TESTING=OFF >/dev/null

    ui_info "Building mag-usb"
    cmake --build "$build" --target mag-usb --parallel "$(nproc)" >/dev/null

    ui_info "Installing mag-usb to $PREFIX/bin/"
    install -d "$PREFIX/bin"
    install -m 0755 "$build/mag-usb" "$PREFIX/bin/mag-usb"

    echo "$stamp_content" > "$stamp"
}

write_provenance() {
    local src="$1"
    local sidecar="$PREFIX/bin/mag-usb.provenance"
    local tmp="${sidecar}.tmp"

    local src_sha src_ref
    src_sha=$(git -C "$src" rev-parse HEAD)
    src_ref=$(git -C "$src" rev-parse --abbrev-ref HEAD)
    # If we landed on a detached HEAD (tag checkout), use the configured ref.
    [[ "$src_ref" == "HEAD" ]] && src_ref="$MAG_USB_REF"

    local builder_sha
    if builder_sha=$(git -C "$PREFIX" rev-parse HEAD 2>/dev/null); then :; else builder_sha="unknown"; fi

    # mag-usb -V prints config-not-found warnings to stdout before the
    # version line; grep for the "Version:" prefix specifically.
    local version
    version=$("$PREFIX/bin/mag-usb" -V 2>&1 | awk '/^Version:/ {print $2; exit}')
    [[ -z "$version" ]] && version="unknown"

    local glibc_ver
    glibc_ver=$(ldd --version 2>&1 | head -1 | awk '{print $NF}')

    local os_pretty kernel arch cmake_ver gcc_ver
    os_pretty=$(. /etc/os-release && echo "$PRETTY_NAME")
    kernel=$(uname -r)
    arch=$(uname -m)
    cmake_ver=$(cmake --version | head -1 | awk '{print $3}')
    gcc_ver=$(gcc -dumpfullversion 2>/dev/null || gcc -dumpversion)

    local host_id
    host_id=$(hostname -s)

    local build_date
    build_date=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    # Runtime apt deps (pinned to our ENABLE_WEBSOCKET=ON build profile).
    # libc6 is intentionally omitted — `glibc:` above records the version.
    cat > "$tmp" <<EOF
# bin/mag-usb.provenance — auto-generated by scripts/build-mag-usb.sh
# Schema: sigmond/docs/native-binaries.md
binary: mag-usb
version: "${version}"

upstream:
  - name: mag-usb
    url:  ${MAG_USB_URL}
    ref:  ${src_ref}
    sha:  ${src_sha}

build:
  host:        "${host_id}"
  os:          "${os_pretty}"
  kernel:      "${kernel}"
  arch:        ${arch}
  glibc:       "${glibc_ver}"
  cmake:       "${cmake_ver}"
  gcc:         "${gcc_ver}"
  date:        ${build_date}
  builder:     "build-mag-usb.sh"
  builder_sha: "${builder_sha}"

runtime:
  needs_apt:
    - libstdc++6   # ENABLE_WEBSOCKET=ON pulls in the C++ standard library
    - libgcc-s1
  rpath: []
EOF
    mv "$tmp" "$sidecar"
    ui_info "Wrote provenance sidecar -> $sidecar"
}

main() {
    ensure_apt_deps

    mkdir -p "$BUILD_DIR" "$PREFIX/bin"

    local mag_usb_src="$BUILD_DIR/mag-usb"

    clone_or_update "$MAG_USB_URL" "$MAG_USB_REF" "$mag_usb_src"
    build_mag_usb "$mag_usb_src"
    write_provenance "$mag_usb_src"

    if ! "$PREFIX/bin/mag-usb" -V >/dev/null 2>&1; then
        ui_error "mag-usb built but failed -V sanity check"
        exit 1
    fi
    ui_info "Build complete. mag-usb is at $PREFIX/bin/mag-usb"
    "$PREFIX/bin/mag-usb" -V 2>&1 | head -1 | sed 's/^/[INFO]  /'
}

main
