#!/bin/bash
# build-mag-usb.sh — idempotent from-source build of mag-usb
#
# Usage: sudo ./scripts/build-mag-usb.sh [--force] [--no-apt]
#
# Clones HamSCI/mag-usb into a scratch dir, builds the C executable with
# cmake, installs it to <repo>/bin/mag-usb, and writes
# <repo>/bin/mag-usb.provenance.
#
# PINNED, deliberately.  master mirrors wittend/master, which is v0.0.9.
# A 2026-08-07 continuity check against our RM3100 archive measured v0.0.9
# reading 1.31% LOW on all three axes relative to v0.0.6 -- getCCGainEquiv()
# returns 148 for CC=400 where the older build used the datasheet 150.  That
# is a real step change in the science record, not a calibration improvement
# we asked for, so we hold at the last ref we have archive continuity for
# until it is resolved with upstream.  Do NOT move this to master casually:
# nothing in the build or at runtime will complain, the readings just quietly
# shift by 1.31%.
#
# We no longer carry a patch branch.  The fixes we contributed in May 2026
# — the -f config-file flag this recorder depends on, -A address override,
# -P register readback, and programming the CC/NOS registers on-chip — are
# all upstream as of wittend/master 6e660577.  The only thing the old
# sigmond-integration branch still changed was ENABLE_WEBSOCKET's default,
# and this script sets that explicitly below, so the branch bought nothing
# but drift.  It is tagged sigmond-integration-retired-20260807 if the
# history is ever needed.
#
# Skips work that is already up to date.
#
# Honors these env vars:
#   MAG_RECORDER_PREFIX     install prefix         (default: /opt/git/sigmond/mag-recorder)
#   MAG_RECORDER_BUILD_DIR  scratch build dir      (default: /var/cache/mag-recorder/build)
#   MAG_USB_URL             override remote        (default: https://github.com/HamSCI/mag-usb.git)
#   MAG_USB_REF             git ref                (default: the pinned tag below)
#
# After a successful run, ${PREFIX}/bin/mag-usb is on disk, reports
# its version cleanly, and a YAML provenance sidecar is alongside it.
#
# Convention: see sigmond/docs/native-binaries.md.

set -euo pipefail

PREFIX="${MAG_RECORDER_PREFIX:-/opt/git/sigmond/mag-recorder}"
BUILD_DIR="${MAG_RECORDER_BUILD_DIR:-/var/cache/mag-recorder/build}"
MAG_USB_URL="${MAG_USB_URL:-https://github.com/HamSCI/mag-usb.git}"
# See the header: pinned to the v0.0.6 lineage (sha 76c7b7c) for archive
# continuity, NOT because the fork is still alive.  Override to build
# something else, e.g. MAG_USB_REF=master to evaluate bare upstream.
# v0.0.9-sigmond.1 = wittend/master v0.0.9 (6e660577) + the three PRs
# offered upstream (wittend#10 CC/NOS init, #11 TLS verify, #12 parser
# hardening).  Drop back to plain master once Dave merges them.
MAG_USB_REF="${MAG_USB_REF:-v0.0.9-sigmond.1}"
# WebSocket output is OFF: MQTT (upstream v0.0.9) supersedes it as the
# real-time path — broker-mediated, so only the broker needs exposing and
# clients can live anywhere, rather than each station serving sockets.
# Turning it off also drops the C++11 toolchain and the vendored
# third_party/mengrao-websocket dependency from the build, and libstdc++6 /
# libgcc-s1 from the runtime.  Set MAG_USB_ENABLE_WEBSOCKET=ON to restore.
ENABLE_WEBSOCKET="${MAG_USB_ENABLE_WEBSOCKET:-OFF}"

APT_DEPS=(
    # libssl-dev: upstream compiles src/mqtt_client.c into mag-usb
    # unconditionally and its CMakeLists does find_package(OpenSSL REQUIRED),
    # so OpenSSL headers are now a hard build dependency even though MQTT
    # itself stays off at runtime (mqtt_enable defaults FALSE).
    #
    # build-essential is kept for the C toolchain.  It also carries g++,
    # which is only needed if ENABLE_WEBSOCKET is turned back ON — see the
    # cmake invocation below for why it is OFF.
    build-essential cmake pkg-config git libssl-dev
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
    # BUILD_TESTING=OFF skips the in-tree test executables (including the
    # MQTT listener/command tools); sigmond doesn't ship them.
    ui_info "  ENABLE_WEBSOCKET=$ENABLE_WEBSOCKET"
    cmake -S "$src" -B "$build" \
        -DCMAKE_BUILD_TYPE=Release \
        -DENABLE_WEBSOCKET="$ENABLE_WEBSOCKET" \
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

    # Each of these captures the command's whole output into a variable
    # first, then parses it.  Piping a long-running producer straight into
    # `head -1` (or `awk ... exit`) closes the pipe early, the producer
    # takes SIGPIPE, and under `set -o pipefail` above that aborts the
    # script.  That is not theoretical: on Debian 13 this function died at
    # exit 141 immediately after the binary was installed, so the artifact
    # landed but the provenance sidecar was silently never written.
    #
    # mag-usb -V prints config-not-found warnings to stdout before the
    # version line, so match the "Version:" prefix specifically and keep
    # the first hit (awk without `exit`, so the producer is fully drained).
    local version_out version
    version_out=$("$PREFIX/bin/mag-usb" -V 2>&1 || true)
    version=$(printf '%s\n' "$version_out" \
        | awk '/^Version:/ && !seen {v=$2; seen=1} END {print v}')
    [[ -z "$version" ]] && version="unknown"

    local ldd_out glibc_ver
    ldd_out=$(ldd --version 2>&1 || true)
    glibc_ver=$(printf '%s\n' "$ldd_out" | awk 'NR==1 {print $NF}')

    local os_pretty kernel arch cmake_ver gcc_ver
    os_pretty=$(. /etc/os-release && echo "$PRETTY_NAME")
    kernel=$(uname -r)
    arch=$(uname -m)
    local cmake_out
    cmake_out=$(cmake --version 2>&1 || true)
    cmake_ver=$(printf '%s\n' "$cmake_out" | awk 'NR==1 {print $3}')
    gcc_ver=$(gcc -dumpfullversion 2>/dev/null || gcc -dumpversion)

    local host_id
    host_id=$(hostname -s)

    local build_date
    build_date=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    # Runtime apt deps follow the build profile above.  The C++ runtime is
    # only pulled in by the WebSocket bridge (src/ws_bridge.cpp), so with
    # ENABLE_WEBSOCKET=OFF this is a pure-C binary.  libc6 is intentionally
    # omitted — `glibc:` above records the version.  OpenSSL is linked for
    # MQTT, so libssl3 is a runtime dep regardless of the websocket switch.
    local needs_apt
    if [[ "$ENABLE_WEBSOCKET" == "ON" ]]; then
        needs_apt=$'    - libstdc++6   # ENABLE_WEBSOCKET=ON pulls in the C++ standard library\n    - libgcc-s1\n    - libssl3      # OpenSSL, linked for MQTT (mqtt_client.c)'
    else
        needs_apt=$'    - libssl3      # OpenSSL, linked for MQTT (mqtt_client.c)'
    fi
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
${needs_apt}
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
    local final_out
    final_out=$("$PREFIX/bin/mag-usb" -V 2>&1 || true)
    printf '%s\n' "$final_out" | awk 'NR==1 {print "[INFO]  " $0}'
}

main
