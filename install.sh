#!/bin/bash
#
# mag-recorder installation/upgrade script
#
# Idempotent.  Installs or upgrades:
#   - magrec service user (dialout group for /dev/ttyMAG0 access)
#   - upstream mag-usb C binary built from /opt/git/sigmond/mag-usb
#   - /etc/udev/rules.d/99-PololuI2C.rules (stable /dev/ttyMAG0 symlink)
#   - Python venv at /opt/mag-recorder/venv
#   - Rendered config at /etc/mag-recorder/mag-recorder-config.toml
#   - Systemd units (continuous daemon + daily upload timer)
#
# Usage:
#   sudo ./install.sh              # install or upgrade
#   sudo ./install.sh --uninstall  # remove
#

set -e

INSTALL_DIR="/opt/mag-recorder"
CONFIG_DIR="/etc/mag-recorder"
RUN_DIR="/run/mag-recorder"          # created by systemd RuntimeDirectory
SPOOL_DIR="/var/lib/mag-recorder"
LOG_DIR="/var/log/mag-recorder"
SERVICE_USER="magrec"
SERVICE_GROUP="magrec"

# Where mag-usb is cloned.  install.sh builds from this checkout so
# the binary always reflects whatever sigmond-integration commit the
# operator pulled.  Override with MAG_USB_REPO=/path on the command line.
MAG_USB_REPO="${MAG_USB_REPO:-/opt/git/sigmond/mag-usb}"

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

check_root() {
    [[ $EUID -eq 0 ]] || error "Run as root (sudo)."
}

_ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        info "  uv $(uv --version 2>/dev/null | awk '{print $2}') at $(command -v uv)"
        return
    fi
    info "  uv not found -- installing system-wide to /usr/local/bin"
    command -v curl >/dev/null || error "curl not found (apt install curl)"
    if ! curl -LsSf https://astral.sh/uv/install.sh | env XDG_BIN_HOME=/usr/local/bin UV_NO_MODIFY_PATH=1 sh; then
        error "uv installer failed"
    fi
    command -v uv >/dev/null || error "uv installer ran but uv is still not on PATH"
    info "  uv $(uv --version 2>/dev/null | awk '{print $2}') installed"
}

check_dependencies() {
    info "Checking dependencies..."
    command -v python3 >/dev/null || error "python3 not found"
    command -v cmake   >/dev/null || error "cmake not found (apt install cmake)"
    command -v gcc     >/dev/null || error "gcc not found (apt install build-essential)"
    _ensure_uv
    # whiptail is the config wizard UI but mag-recorder still works
    # without it (stdin-prompt fallback), so warn rather than error.
    if ! command -v whiptail >/dev/null; then
        warn "whiptail not installed -- the interactive config wizard"
        warn "  (mag-recorder config init|edit) will fall back to the"
        warn "  legacy stdin-prompt path.  apt install whiptail to enable."
    fi
}

create_user() {
    info "Creating service user ${SERVICE_USER}..."
    if id "$SERVICE_USER" &>/dev/null; then
        info "  ${SERVICE_USER} already exists"
    else
        # --no-create-home keeps /home small, but we DO need a HOME
        # directory for ssh's known_hosts (the PSWS sftp uploader runs
        # with StrictHostKeyChecking=accept-new, which writes the
        # server's pinned host key to $HOME/.ssh/known_hosts on first
        # contact).  Create the home dir explicitly below instead of
        # via useradd's skel-copy machinery so /etc/skel doesn't leak in.
        useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
        info "  created ${SERVICE_USER}"
    fi
    install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0700 "/home/${SERVICE_USER}" "/home/${SERVICE_USER}/.ssh"
    # dialout owns /dev/ttyMAG0 (mode 0660 root:dialout per the udev rule).
    # Without this membership the daemon can't open the adapter, and the
    # systemd unit's SupplementaryGroups=dialout has nothing to attach.
    if getent group dialout >/dev/null; then
        if ! id -nG "$SERVICE_USER" | grep -qw dialout; then
            usermod -a -G dialout "$SERVICE_USER"
            info "  added ${SERVICE_USER} to dialout"
        else
            info "  ${SERVICE_USER} already in dialout"
        fi
    else
        warn "  dialout group not present on this host"
    fi
}

build_and_install_mag_usb() {
    info "Building and installing mag-usb from ${MAG_USB_REPO}..."
    [[ -d "$MAG_USB_REPO" ]] || error "mag-usb repo not found at ${MAG_USB_REPO}.
    Clone wittend/mag-usb (sigmond-integration branch) there, or pass MAG_USB_REPO=/path."

    ( cd "$MAG_USB_REPO" && \
        cmake -S . -B build -DCMAKE_BUILD_TYPE=Release >/dev/null && \
        cmake --build build --target mag-usb -j >/dev/null )

    install -m 0755 "$MAG_USB_REPO/build/mag-usb" /usr/local/bin/mag-usb
    info "  installed /usr/local/bin/mag-usb ($(\
        /usr/local/bin/mag-usb -V 2>&1 | grep -i version | head -1 | tr -d '\n'))"
}

install_udev_rule() {
    info "Installing udev rule for Pololu USB-I2C adapter..."
    install -m 0644 "$REPO_ROOT/install/99-PololuI2C.rules" /etc/udev/rules.d/99-PololuI2C.rules
    udevadm control --reload-rules
    udevadm trigger
    info "  /dev/ttyMAG0 will resolve to whichever ttyACMn the Pololu enumerates as"
}

create_dirs() {
    info "Creating spool / log dirs..."
    install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0755 "$SPOOL_DIR" \
                                                              "$SPOOL_DIR/upload" \
                                                              "$LOG_DIR"
    install -d                                          -m 0755 "$CONFIG_DIR"
}

install_application() {
    info "Installing Python application to ${INSTALL_DIR}..."
    if [[ ! -d "$INSTALL_DIR/venv" ]]; then
        install -d -m 0755 "$INSTALL_DIR"
        # --seed populates pip/setuptools/wheel for compatibility with
        # tooling that shells out to pip; harmless overhead otherwise.
        uv venv "$INSTALL_DIR/venv" --python 3.11 --seed --quiet
    fi
    # Pre-clean any leftover egg-info from prior dev installs in the
    # source tree -- if it's owned by a different user, setuptools'
    # "Cannot update time stamp" check inside the build sandbox would
    # abort the editable install.  Safe to delete; uv recreates it.
    rm -rf "$REPO_ROOT/src/mag_recorder.egg-info" \
           "$REPO_ROOT/build" \
           "$REPO_ROOT"/*.egg-info 2>/dev/null || true
    # hs-uploader is a sibling sigmond repo (not on PyPI).  pyproject.toml's
    # [tool.uv.sources] resolves `hs-uploader = { path = "../hs-uploader" }`
    # relative to mag-recorder, which equals /opt/git/sigmond/hs-uploader on
    # the canonical layout.  uv sync honors that natively, so no manual
    # sibling pre-install is needed (unlike the old pip-based flow).
    local hs_uploader_repo="${HS_UPLOADER_REPO:-/opt/git/sigmond/hs-uploader}"
    if [[ ! -d "$hs_uploader_repo" ]]; then
        error "hs-uploader repo not found at $hs_uploader_repo -- uv sync will fail.
    Clone https://github.com/mijahauan/hs-uploader to /opt/git/sigmond/hs-uploader,
    or pass HS_UPLOADER_REPO=/path."
    fi
    rm -rf "$hs_uploader_repo/src"/*.egg-info "$hs_uploader_repo"/*.egg-info 2>/dev/null || true
    # uv sync reads pyproject.toml + uv.lock, resolves [tool.uv.sources]
    # to local sibling paths, installs mag-recorder editable into the
    # venv, and pins exactly what's in uv.lock.  --no-dev skips dev
    # extras (pytest etc.); --frozen requires uv.lock to be current
    # (regenerate locally with `uv lock` if siblings have shifted).
    UV_PROJECT_ENVIRONMENT="$INSTALL_DIR/venv" \
        uv sync --project "$REPO_ROOT" --frozen --no-dev --quiet
    # Non-canonical HS_UPLOADER_REPO override (rare; dev convenience):
    # uv pip install -e replaces the path-resolved install with the
    # operator's chosen location.  uv pip install needs --python (not
    # UV_PROJECT_ENVIRONMENT, which only applies to project-level
    # commands like uv sync).
    if [[ "$hs_uploader_repo" != "/opt/git/sigmond/hs-uploader" ]]; then
        uv pip install --quiet --python "$INSTALL_DIR/venv/bin/python3" -e "$hs_uploader_repo"
    fi
    # sigmond is the host-wide orchestrator; mag-recorder lazy-imports
    # sigmond.wizard_dispatch from configurator.py for the whiptail
    # wizard plumbing (helpers shared with psk-recorder / wspr-recorder
    # via sigmond's lib).  Falls back to a local implementation when
    # absent, so this install is recommended but not strictly required.
    # NOT declared in pyproject.toml so uv sync doesn't install it;
    # we add it explicitly when the sibling exists.
    local sigmond_repo="${SIGMOND_REPO:-/opt/git/sigmond/sigmond}"
    if [[ -d "$sigmond_repo" ]]; then
        rm -rf "$sigmond_repo"/*.egg-info 2>/dev/null || true
        # uv pip install needs --python (UV_PROJECT_ENVIRONMENT only works for uv sync).
        uv pip install --quiet --python "$INSTALL_DIR/venv/bin/python3" -e "$sigmond_repo"
    else
        warn "  sigmond repo not found at $sigmond_repo -- wizard will use the local"
        warn "  legacy-fallback dispatch.  Clone sigmond, or pass SIGMOND_REPO=/path."
    fi
    # CONTRACT v0.6 §12.5 (Pattern A): the service user must be able
    # to traverse the repo to import the package in editable mode.
    if ! sudo -u "$SERVICE_USER" test -r "$REPO_ROOT/src/mag_recorder/__init__.py"; then
        error "Service user $SERVICE_USER cannot read $REPO_ROOT/src/mag_recorder/__init__.py.
    Fix: ensure the repo lives at /opt/git/sigmond/mag-recorder (the canonical, group-readable
    location), or chmod g+rx the path and ensure $SERVICE_USER is in the owner's group."
    fi
    # Symlink the venv entry point so `mag-recorder` works on $PATH.
    ln -sfn "$INSTALL_DIR/venv/bin/mag-recorder" /usr/local/bin/mag-recorder
    info "  $(/usr/local/bin/mag-recorder version --json 2>/dev/null | head -1 || echo 'mag-recorder installed')"
}

install_config() {
    info "Installing config template..."
    if [[ ! -f "$CONFIG_DIR/mag-recorder-config.toml" ]]; then
        install -m 0644 "$REPO_ROOT/config/mag-recorder-config.toml.template" \
                        "$CONFIG_DIR/mag-recorder-config.toml"
        info "  rendered $CONFIG_DIR/mag-recorder-config.toml (edit before starting!)"
    else
        info "  $CONFIG_DIR/mag-recorder-config.toml already present (not overwritten)"
    fi
}

install_systemd_units() {
    info "Installing systemd units..."
    for u in mag-recorder.service mag-recorder-upload.service mag-recorder-upload.timer; do
        ln -sfn "$REPO_ROOT/systemd/$u" "/etc/systemd/system/$u"
    done
    systemctl daemon-reload
    # mag-recorder.service is enabled by sigmond's deploy.toml [systemd].units
    # at apply time.  The upload timer is deliberately NOT auto-enabled --
    # operators turn it on with `systemctl enable --now mag-recorder-upload.timer`
    # once they're ready for PSWS uploads.
    info "  units linked; enable mag-recorder.service when ready"
}

uninstall() {
    info "Removing mag-recorder..."
    systemctl disable --now mag-recorder.service mag-recorder-upload.timer 2>/dev/null || true
    rm -f /etc/systemd/system/mag-recorder.service \
          /etc/systemd/system/mag-recorder-upload.service \
          /etc/systemd/system/mag-recorder-upload.timer \
          /usr/local/bin/mag-recorder \
          /usr/local/bin/mag-usb \
          /etc/udev/rules.d/99-PololuI2C.rules
    systemctl daemon-reload || true
    udevadm control --reload-rules 2>/dev/null || true
    info "Removed binaries, units, udev rule."
    info "Kept (delete by hand if desired): ${INSTALL_DIR}, ${SPOOL_DIR}, ${LOG_DIR}, ${CONFIG_DIR}, user '${SERVICE_USER}'."
}

main() {
    check_root
    if [[ "${1:-}" == "--uninstall" ]]; then
        uninstall
        return
    fi
    check_dependencies
    create_user
    build_and_install_mag_usb
    install_udev_rule
    create_dirs
    install_application
    install_config
    install_systemd_units
    info "Install complete.  Next:"
    info "  1. edit /etc/mag-recorder/mag-recorder-config.toml"
    info "  2. systemctl start mag-recorder.service"
    info "  3. journalctl -u mag-recorder -f"
}

main "$@"
