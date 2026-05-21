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

check_dependencies() {
    info "Checking dependencies..."
    command -v python3 >/dev/null || error "python3 not found"
    command -v cmake   >/dev/null || error "cmake not found (apt install cmake)"
    command -v gcc     >/dev/null || error "gcc not found (apt install build-essential)"
    python3 -c "import venv" 2>/dev/null || error "python3-venv missing (apt install python3-venv)"
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
        python3 -m venv "$INSTALL_DIR/venv"
    fi
    # Pre-clean any leftover egg-info from prior dev installs in the
    # source tree -- if it's owned by a different user, setuptools'
    # "Cannot update time stamp" check inside the build sandbox would
    # abort the editable install.  Safe to delete; pip recreates it.
    rm -rf "$REPO_ROOT/src/mag_recorder.egg-info" \
           "$REPO_ROOT/build" \
           "$REPO_ROOT"/*.egg-info 2>/dev/null || true
    # Run pip as root (we already require root for the whole script).
    # The venv is then root-owned; the daemon (running as magrec) only
    # needs to READ it -- matches the psk-recorder / wspr-recorder
    # pattern.  Editable mode means the source tree is the canonical
    # location; updating /opt/git/sigmond/mag-recorder + `systemctl
    # restart mag-recorder` is the upgrade flow.
    "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip setuptools wheel
    # hs-uploader is a sibling sigmond repo (not on PyPI).  pyproject.toml
    # declares it via [tool.uv.sources] which plain pip ignores, so we
    # install the sibling editable into the venv before pip processes
    # mag-recorder's dependency resolution.  Same pattern psk-recorder uses.
    local hs_uploader_repo="${HS_UPLOADER_REPO:-/opt/git/sigmond/hs-uploader}"
    if [[ -d "$hs_uploader_repo" ]]; then
        rm -rf "$hs_uploader_repo/src"/*.egg-info "$hs_uploader_repo"/*.egg-info 2>/dev/null || true
        "$INSTALL_DIR/venv/bin/pip" install --quiet -e "$hs_uploader_repo"
    else
        warn "  hs-uploader repo not found at $hs_uploader_repo -- mag-recorder pip install will fail."
        warn "  Clone it there, or pass HS_UPLOADER_REPO=/path."
    fi
    "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade -e "$REPO_ROOT"
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
