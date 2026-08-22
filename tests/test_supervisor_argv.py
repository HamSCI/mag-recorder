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


# --- WebSocket liveness (2026-08-22): the bundled mag-usb was built with
# ENABLE_WEBSOCKET=OFF while the supervisor passed -W/-w/-a, and the flags were
# silently ignored — nothing listened on 8765 for weeks.  The supervisor must
# be able to TELL, and say so loudly. ---

def test_websocket_listening_true_for_a_real_listener():
    import socket
    from mag_recorder.core.supervisor import websocket_listening
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    try:
        port = srv.getsockname()[1]
        assert websocket_listening("127.0.0.1", port, timeout=1.0) is True
    finally:
        srv.close()


def test_websocket_listening_false_when_nothing_listens():
    import socket
    from mag_recorder.core.supervisor import websocket_listening
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    assert websocket_listening("127.0.0.1", port, timeout=0.5) is False


def test_websocket_listening_treats_wildcard_bind_as_loopback():
    import socket
    from mag_recorder.core.supervisor import websocket_listening
    srv = socket.socket(); srv.bind(("0.0.0.0", 0)); srv.listen(1)
    try:
        port = srv.getsockname()[1]
        assert websocket_listening("0.0.0.0", port, timeout=1.0) is True
    finally:
        srv.close()


def test_websocket_expectation_message_names_the_fix():
    """What the supervisor logs when -W was asked for but no listener appears:
    it must name the cause (binary built without ENABLE_WEBSOCKET) and the fix."""
    from mag_recorder.core.supervisor import websocket_missing_message
    msg = websocket_missing_message("127.0.0.1", 8765)
    assert "8765" in msg and "ENABLE_WEBSOCKET" in msg and "MAG_USB_ENABLE_WEBSOCKET=ON" in msg
