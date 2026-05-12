"""CONTRACT v0.6 §16 + §17 inventory and validate JSON builders.

mag-recorder is a non-radiod data-source client (§16.5):
  - omits radiod_id, data_destination, chain_delay_ns_applied
  - declares data_path = {kind: "other", details: {...}}
  - participates in §3, §10, §11, §13, §14, §17 normally
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

from mag_recorder import __version__
from mag_recorder.version import GIT_INFO

logger = logging.getLogger(__name__)

CONTRACT_VERSION = "0.6"

_INSTANCE = "default"


def _data_path(config: dict) -> dict:
    mag = config.get("mag", {})
    return {
        "kind": "other",
        "details": {
            "description":     "RM3100 magnetometer via Pololu USB-I2C adapter",
            "device":          mag.get("device", "/dev/ttyMAG0"),
            "upstream_binary": mag.get("mag_usb_binary", "/usr/local/bin/mag-usb"),
            "sensor":          "PNI RM3100",
            "transport":       "cdc_acm (USB CDC-ACM) -> i2c-pololu",
            "sample_hz":       int(mag.get("sample_hz", 1)),
        },
    }


def _data_sinks(config: dict) -> list[dict]:
    paths = config.get("paths", {})
    spool = paths.get("spool_dir", "/var/lib/mag-recorder")
    log_dir = paths.get("log_dir", "/var/log/mag-recorder")

    # Rough sizing: 1 sample/s * ~100 bytes/line * 86400 s/day = ~8.6 MB/day
    # uncompressed JSONL.  Compressed (the zip we upload) is much smaller
    # but the spool itself is the uncompressed daily file.
    sinks: list[dict] = [
        {
            "kind":           "file",
            "target":         spool,
            "schema_ref":     None,
            "retention_days": 7,
            "mb_per_day":     10,
        },
        {
            "kind":           "file",
            "target":         log_dir,
            "schema_ref":     None,
            "retention_days": 365,
            "mb_per_day":     1,
        },
    ]
    return sinks


def build_inventory(config: dict, config_path: Path) -> dict:
    """Build the `inventory --json` payload."""
    station = config.get("station", {})
    paths = config.get("paths", {})
    log_dir = paths.get("log_dir", "/var/log/mag-recorder")

    instance = {
        "instance":                    _INSTANCE,
        "host":                        os.uname().nodename,
        "data_path":                   _data_path(config),
        "data_sinks":                  _data_sinks(config),
        "uses_timing_calibration":     False,
        "provides_timing_calibration": False,
        "psws_station_id":             station.get("psws_station_id", ""),
        "instrument_id":               station.get("instrument_id", "RM3100"),
    }

    log_paths = {
        _INSTANCE: {
            "process": f"{log_dir}/mag-recorder.log",
            "samples": f"{log_dir}/samples.jsonl",
            "misses":  f"{log_dir}/missed-samples.log",
        }
    }

    effective_level = logging.getLogger().getEffectiveLevel()
    log_level_name = logging.getLevelName(effective_level)

    payload: dict[str, Any] = {
        "client":           "mag-recorder",
        "version":          __version__,
        "contract_version": CONTRACT_VERSION,
        "config_path":      str(config_path),
    }
    if GIT_INFO:
        payload["git"] = GIT_INFO
    payload["log_paths"] = log_paths
    payload["log_level"] = log_level_name
    payload["instances"] = [instance]
    payload["deps"] = {
        "external_binaries": [
            {"name": "mag-usb",
             "path": config.get("mag", {}).get("mag_usb_binary", "/usr/local/bin/mag-usb"),
             "note": "github.com/wittend/mag-usb (sigmond-integration patches)"},
        ],
        "pypi": [
            {"name": "hs-uploader", "note": "PSWS upload pipeline (transport TODO)"},
        ],
    }
    payload["issues"] = _collect_issues(config)
    return payload


def build_validate(config: dict, config_path: Path | None = None) -> dict:
    """Build the `validate --json` payload."""
    issues = _collect_issues(config)
    payload: dict[str, Any] = {
        "ok": not any(i["severity"] == "fail" for i in issues),
    }
    if config_path is not None:
        payload["config_path"] = str(config_path)
    payload["issues"] = issues
    return payload


def _collect_issues(config: dict) -> list[dict]:
    issues: list[dict] = []
    station = config.get("station", {})
    mag = config.get("mag", {})
    uploader = config.get("uploader", {})
    simulator = config.get("simulator", {})

    # §12.3: required station identity for PSWS uploads.
    if not station.get("psws_station_id") or \
       station["psws_station_id"].startswith("<"):
        issues.append({
            "severity": "fail",
            "instance": _INSTANCE,
            "message": "station.psws_station_id is unset (need PSWS-issued S0xxxxx)",
        })
    if not station.get("instrument_id"):
        issues.append({
            "severity": "fail",
            "instance": _INSTANCE,
            "message": "station.instrument_id is empty",
        })
    if not station.get("callsign") or station["callsign"].startswith("<"):
        issues.append({
            "severity": "warn",
            "instance": _INSTANCE,
            "message": "station.callsign is unset",
        })
    if not station.get("grid_square") or station["grid_square"].startswith("<"):
        issues.append({
            "severity": "warn",
            "instance": _INSTANCE,
            "message": "station.grid_square is unset",
        })

    # mag-usb binary must exist on PATH unless we're running in
    # simulator mode (no hardware required for the simulator).
    sim_on = bool(simulator.get("enabled")) or \
             os.environ.get("MAG_RECORDER_SIMULATE", "").lower() in ("1", "true", "yes")
    mag_bin = mag.get("mag_usb_binary", "/usr/local/bin/mag-usb")
    if not sim_on:
        if not shutil.which(mag_bin) and not Path(mag_bin).is_file():
            issues.append({
                "severity": "fail",
                "instance": _INSTANCE,
                "message": f"mag-usb binary not found: {mag_bin} "
                           f"(install upstream from github.com/wittend/mag-usb, "
                           f"or enable [simulator].enabled for testing)",
            })
        device = mag.get("device", "/dev/ttyMAG0")
        if not Path(device).exists():
            issues.append({
                "severity": "warn",
                "instance": _INSTANCE,
                "message": f"adapter device not present: {device} "
                           f"(udev rule installed? adapter plugged in?)",
            })

    # Upload prerequisites: SSH key + non-empty user.
    if uploader.get("enabled", True):
        ssh_key = uploader.get("ssh_key_file", "")
        if ssh_key and not Path(ssh_key).is_file():
            issues.append({
                "severity": "warn",
                "instance": _INSTANCE,
                "message": f"uploader.ssh_key_file does not exist: {ssh_key} "
                           f"(shared with hf-timestd Grape uploader; "
                           f"ssh-keygen + register key on PSWS portal first)",
            })
        if not uploader.get("user"):
            issues.append({
                "severity": "fail",
                "instance": _INSTANCE,
                "message": "uploader.user is empty and no station.psws_station_id to fall back to",
            })

    return issues
