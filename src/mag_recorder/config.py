"""TOML config loader, defaults, env-var fallbacks for mag-recorder.

Mirrors psk-recorder's config pattern: a single dict returned from
``load_config()`` with merged defaults.  Env vars
``MAG_RECORDER_*`` override matching TOML fields, which matches the
§14 env-var bag convention sigmond uses to seed config interviews.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python <3.11
    import tomli as tomllib  # type: ignore[no-redef]


DEFAULT_CONFIG_PATH = Path("/etc/mag-recorder/mag-recorder-config.toml")

DEFAULTS: dict[str, Any] = {
    "station": {
        "psws_station_id": "",
        "instrument_id":   "RM3100",
        "callsign":        "",
        "grid_square":     "",
        "latitude":        0.0,
        "longitude":       0.0,
        "elevation_m":     0.0,
        "description":     "RM3100 magnetometer via Pololu USB-I2C",
    },
    "mag": {
        # Path to the upstream mag-usb binary.  Override if installed
        # somewhere non-standard.
        "mag_usb_binary":     "/usr/local/bin/mag-usb",
        # Pololu adapter device.  Default matches install/99-PololuI2C.rules.
        "device":             "/dev/ttyMAG0",
        # RM3100 I2C address (0x20..0x23 depending on AD0/AD1 strapping
        # on the carrier board).  TangerineSDR / CWRU boards: 0x23.
        # PNI eval boards: 0x20.
        "i2c_address":        0x23,
        # Sample cadence -- mag-usb produces 1 sample / UTC second.
        # Setting this to anything other than 1 currently requires
        # patching mag-usb; left here for forward compatibility.
        "sample_hz":          1,
        # Driver-config path mag-recorder renders at daemon startup and
        # passes to mag-usb via `-f`.  /run/mag-recorder/ is provided by
        # systemd's RuntimeDirectory=mag-recorder in the unit file
        # (cleaned at every stop, owner magrec:magrec).  Override only
        # for tests / ad-hoc runs.
        "driver_config_path": "/run/mag-recorder/mag-usb-driver.toml",
        # Advanced tuning -- defaults match wittend/mag-usb tools/config.toml
        # and are now actually programmed on the chip (mag-usb PR #1).
        # Most operators won't need to touch these.
        "cycle_count":        400,    # RM3100 cycle count per axis (1..800)
        "nos":                60,     # NOS register (averaging)
        "tmrc_rate":          0x96,   # TMRC sample-rate register
        "drdy_delay_ms":      10,
        "sampling_mode":      "POLL", # "POLL" or "CMM"
        "remote_temp_address": 0x1F,  # MCP9808
        # Per-axis orientation rotations in 90° increments; -180/-90/0/90/180.
        "orientation":        {"x": 0, "y": 0, "z": 0},
    },
    "websocket": {
        # When enabled, mag-recorder launches mag-usb with `-W` so it
        # broadcasts each JSON sample line over a WebSocket server.
        "enable":         False,
        "bind_address":   "0.0.0.0",
        "port":           8765,
    },
    "paths": {
        "spool_dir":        "/var/lib/mag-recorder",
        "log_dir":          "/var/log/mag-recorder",
        "upload_queue_dir": "/var/lib/mag-recorder/upload",
    },
    "uploader": {
        "enabled":              True,
        "protocol":             "sftp",
        "host":                 "pswsnetwork.eng.ua.edu",
        "user":                 "",
        "ssh_key_file":         "/etc/hs-uploader/keys/id_ed25519",
        "bandwidth_limit_kbps": 100,
        "daily_run_at_utc":     "03:00",
    },
    "simulator": {
        "enabled":        False,
        "baseline_x_nt":  21500.0,
        "baseline_y_nt":   1500.0,
        "baseline_z_nt":  47500.0,
        "noise_nt":           0.5,
        "baseline_rt_c":     22.0,
        "noise_rt_c":         0.1,
    },
}


# §14.3 env-var bag — STATION_* keys are seeded by sigmond's
# configuration interview so the operator only types each fact once.
_ENV_OVERRIDES: list[tuple[str, str, str, type]] = [
    ("STATION_CALLSIGN",        "station", "callsign",         str),
    ("STATION_GRID",            "station", "grid_square",      str),
    ("STATION_LATITUDE",        "station", "latitude",         float),
    ("STATION_LONGITUDE",       "station", "longitude",        float),
    ("STATION_ELEVATION_M",     "station", "elevation_m",      float),
    ("MAG_RECORDER_STATION_ID", "station", "psws_station_id",  str),
    ("MAG_RECORDER_DEVICE",     "mag",     "device",           str),
]


def load_config(path: Path | None = None) -> dict:
    """Load + merge with defaults + env-var overrides."""
    config_path = path or Path(
        os.environ.get("MAG_RECORDER_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    for section, defaults in DEFAULTS.items():
        raw.setdefault(section, {})
        for key, val in defaults.items():
            raw[section].setdefault(key, val)

    for env_key, section, field, cast in _ENV_OVERRIDES:
        env_val = os.environ.get(env_key)
        if env_val:
            try:
                raw[section][field] = cast(env_val)
            except (TypeError, ValueError):
                pass  # bad env value -> keep TOML/default

    # uploader.user defaults to the PSWS station ID when unset.
    if not raw["uploader"]["user"]:
        raw["uploader"]["user"] = raw["station"]["psws_station_id"]

    return raw
