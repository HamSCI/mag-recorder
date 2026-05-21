"""Verify mag-recorder's mag-usb argv construction.

This is the contract with wittend/mag-usb's PR-B CLI surface (-f / -A):
the supervisor must construct argv that points the C binary at the
mag-recorder-owned driver TOML and at the configured I2C address.
"""

from __future__ import annotations

from mag_recorder.core.supervisor import build_mag_usb_argv


def test_argv_minimum() -> None:
    cmd = build_mag_usb_argv(
        binary="/usr/local/bin/mag-usb",
        device="/dev/ttyMAG0",
        i2c_address=0x23,
        driver_config_path="/run/mag-recorder/mag-usb-driver.toml",
    )
    assert cmd == [
        "/usr/local/bin/mag-usb",
        "-O", "/dev/ttyMAG0",
        "-f", "/run/mag-recorder/mag-usb-driver.toml",
        "-A", "0x23",
    ]


def test_argv_includes_websocket_when_enabled() -> None:
    cmd = build_mag_usb_argv(
        binary="/usr/local/bin/mag-usb",
        device="/dev/ttyMAG0",
        i2c_address=0x20,
        driver_config_path="/run/mag-recorder/mag-usb-driver.toml",
        websocket={"enable": True, "port": 9000, "bind_address": "127.0.0.1"},
    )
    # Order matters: -O / -f / -A first, then the WebSocket flags.
    assert cmd[:7] == [
        "/usr/local/bin/mag-usb",
        "-O", "/dev/ttyMAG0",
        "-f", "/run/mag-recorder/mag-usb-driver.toml",
        "-A", "0x20",
    ]
    assert cmd[7:] == ["-W", "-w", "9000", "-a", "127.0.0.1"]


def test_argv_omits_websocket_when_disabled() -> None:
    cmd = build_mag_usb_argv(
        binary="/usr/local/bin/mag-usb",
        device="/dev/ttyMAG0",
        i2c_address=0x23,
        driver_config_path="/run/mag-recorder/mag-usb-driver.toml",
        websocket={"enable": False, "port": 9000},
    )
    assert "-W" not in cmd
    assert "-w" not in cmd


def test_address_renders_as_lowercase_hex() -> None:
    """mag-usb accepts decimal / hex / octal via strtol(base=0); we
    always pass hex so the rendered argv is unambiguous to a human
    reading the journal."""
    cmd = build_mag_usb_argv(
        binary="/usr/local/bin/mag-usb",
        device="/dev/ttyMAG0",
        i2c_address=0x2F,
        driver_config_path="/x.toml",
    )
    assert "-A" in cmd
    assert cmd[cmd.index("-A") + 1] == "0x2f"


def test_address_pads_single_digit_to_two_hex_chars() -> None:
    cmd = build_mag_usb_argv(
        binary="/usr/local/bin/mag-usb",
        device="/dev/ttyMAG0",
        i2c_address=0x3,
        driver_config_path="/x.toml",
    )
    assert cmd[cmd.index("-A") + 1] == "0x03"
