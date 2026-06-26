"""CONTRACT v0.8 §16 + §17 inventory and validate JSON builders.

mag-recorder is a non-radiod data-source client (§16.5):
  - omits radiod_id, data_destination, chain_delay_ns_applied
  - declares data_path = {kind: "other", details: {...}}
  - participates in §3, §10, §11, §13, §14, §17 normally
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from mag_recorder import __version__
from mag_recorder.version import GIT_INFO

logger = logging.getLogger(__name__)

CONTRACT_VERSION = "0.8"

_INSTANCE = "default"


def _path_is_file_or_unreadable(path: Path) -> bool:
    """Return True when `path` is a regular file OR we can't tell.

    `Path.is_file()` raises PermissionError when the parent
    directory isn't traversable by the current uid — happens when
    the operator runs `mag-recorder inventory --json` (or sigmond's
    Overview adapter does so on their behalf) and the SSH key lives
    under another service user's home (e.g. /home/timestd/.ssh/).
    Pre-fix, the inventory and validate JSON builders crashed
    outright instead of producing a payload.

    Treating "permission denied" as "file probably exists" matches
    operator intent: the validate issue we're guarding is "key
    missing", and we can't conclude the key is missing just because
    we can't stat it.  Other unexpected OSErrors propagate.
    """
    try:
        return path.is_file()
    except PermissionError:
        return True
    except FileNotFoundError:
        return False


def _simulator_on(config: dict) -> bool:
    simulator = config.get("simulator", {})
    return bool(simulator.get("enabled")) or \
        os.environ.get("MAG_RECORDER_SIMULATE", "").lower() in ("1", "true", "yes")


def _hardware_present(config: dict) -> bool:
    """CONTRACT §3 / sigmond install-orchestration Phase D — is mag-recorder's
    data source available on this host?

    True when the Pololu USB-I2C adapter device exists (the udev symlink the
    RM3100 is reached through), OR simulator mode is on (the client produces
    synthetic samples and needs no hardware).  Lets sigmond skip / mark
    mag-recorder as core-but-dormant when no magnetometer is attached, from the
    client's own self-describe rather than sigmond probing USB IDs."""
    if _simulator_on(config):
        return True
    device = config.get("mag", {}).get("device", "/dev/ttyMAG0")
    return Path(device).exists()


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
    spool_dir = paths.get("spool_dir", "/var/lib/mag-recorder")

    instance = {
        "instance":                    _INSTANCE,
        "host":                        os.uname().nodename,
        "data_path":                   _data_path(config),
        "data_sinks":                  _data_sinks(config),
        "uses_timing_calibration":     False,
        "provides_timing_calibration": False,
        "psws_station_id":             station.get("psws_station_id", ""),
        "instrument_id":               station.get("instrument_id", "RM3100"),
        # CONTRACT v0.7 §18 — runtime-state field for the §18
        # subscription.  mag-recorder samples at 1 Hz on the host's
        # monotonic clock; there is no radiod-side or upstream
        # timing authority to subscribe to.  Reported as null to
        # satisfy the v0.7 inventory shape and signal "contract-
        # aware, no timing-authority dimension."
        "timing_authority_applied":    None,
    }

    # Sample log lands at <spool_dir>/samples-<UTC-date>.jsonl with
    # one file per UTC day (see core/supervisor.py).  Inventory uses
    # a glob pointer here rather than naming today's file directly so
    # the path stays valid across midnight UTC rollovers — sigmond's
    # log viewer expands the glob when surfacing recent samples.
    log_paths = {
        _INSTANCE: {
            "process": f"{log_dir}/mag-recorder.log",
            "samples": f"{spool_dir}/samples-*.jsonl",
            "upload":  f"{log_dir}/upload.log",
        }
    }

    effective_level = logging.getLogger().getEffectiveLevel()
    log_level_name = logging.getLevelName(effective_level)

    payload: dict[str, Any] = {
        "client":           "mag-recorder",
        "version":          __version__,
        "contract_version": CONTRACT_VERSION,
        "config_path":      str(config_path),
        # CONTRACT §3 / Phase D self-describe: is the magnetometer reachable
        # (or simulator on)?  sigmond consults this to skip / mark dormant.
        "hardware_present": _hardware_present(config),
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

    # Optional chip-readback verification (mag-usb -P, post-PR-C):
    # invoke the C binary against the configured device, parse the
    # "Chip register readback" / "MISMATCH" lines, and surface any
    # divergence between host config and on-chip state.  Off by
    # default because it requires the hardware present and the
    # adapter accessible -- gate with MAG_RECORDER_VALIDATE_CHIP=1
    # so `mag-recorder validate` stays fast in CI / on a build host.
    if not sim_on and os.environ.get("MAG_RECORDER_VALIDATE_CHIP", "").lower() in ("1", "true", "yes"):
        for issue in _chip_readback_issues(mag, config):
            issues.append(issue)

    # Upload prerequisites: SSH key + non-empty user.
    if uploader.get("enabled", True):
        ssh_key = uploader.get("ssh_key_file", "")
        if ssh_key and not _path_is_file_or_unreadable(Path(ssh_key)):
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


# Lines printed by mag-usb -P after wittend/mag-usb PR #3.  We look for the
# "MISMATCH" trailer (any axis disagreement) and for the "(read failed: ...)"
# / "(unavailable: ...)" markers that mean the binary couldn't reach the chip.
_CHIP_MISMATCH_RE = re.compile(
    r"^\s+Chip\s+(?P<field>[A-Za-z][^:]*?)\s*:\s*(?P<line>.+?--\s*MISMATCH)\s*$"
)
_CHIP_READ_FAIL_RE = re.compile(
    r"^\s+Chip\s+(?P<field>[A-Za-z][^:]*?)\s*:\s*\(read failed: (?P<reason>[^)]+)\)\s*$"
)
_CHIP_UNAVAILABLE_RE = re.compile(
    r"^\s+\(unavailable: (?P<reason>[^)]+)\)\s*$"
)


def _chip_readback_issues(mag: dict, config: dict) -> list[dict]:
    """Run `mag-usb -f <driver_toml> -P` and surface host↔chip mismatches.

    Best-effort -- if the binary is missing, the adapter isn't plugged
    in, or the subprocess errors out, we emit a single "warn" issue
    describing what failed and move on.  No exception escapes.
    """
    out: list[dict] = []
    binary = mag.get("mag_usb_binary", "/usr/local/bin/mag-usb")
    device = mag.get("device", "/dev/ttyMAG0")
    addr   = int(mag.get("i2c_address", 0x23))
    if not (shutil.which(binary) or Path(binary).is_file()):
        return out  # already covered by the binary-not-found issue above

    # Render the driver TOML to a temp file (independent of /run/mag-recorder,
    # which doesn't exist when validate is run by a non-systemd user).
    from .core.driver_config import render as _render
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".toml", prefix="mag-usb-driver.", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        _render(config, tmp_path)
        try:
            proc = subprocess.run(
                [binary, "-O", device, "-f", str(tmp_path),
                 "-A", f"0x{addr:02x}", "-P"],
                capture_output=True, text=True, timeout=10,
            )
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
    except Exception as exc:  # subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError
        out.append({
            "severity": "warn",
            "instance": _INSTANCE,
            "message": f"chip-readback validate skipped: {exc.__class__.__name__}: {exc}",
        })
        return out

    if proc.returncode != 0:
        out.append({
            "severity": "warn",
            "instance": _INSTANCE,
            "message": f"mag-usb -P exited {proc.returncode}: "
                       f"{(proc.stderr or proc.stdout).strip().splitlines()[-1:] or ['(no output)']}",
        })

    # Walk stdout for the readback section.  mag-usb mixes diagnostic
    # messages with the chip-readback lines on stdout; we don't care
    # about the host-side block, only the "Chip ..." trailers.
    for line in proc.stdout.splitlines():
        m = _CHIP_MISMATCH_RE.match(line)
        if m:
            out.append({
                "severity": "fail",
                "instance": _INSTANCE,
                "message": f"chip↔host {m.group('field').strip()} disagree: "
                           f"{m.group('line').strip()} -- "
                           f"check that /etc/mag-recorder/mag-recorder-config.toml [mag] keys "
                           f"match what's programmed on the RM3100, then restart mag-recorder",
            })
            continue
        m = _CHIP_READ_FAIL_RE.match(line)
        if m:
            out.append({
                "severity": "fail",
                "instance": _INSTANCE,
                "message": f"mag-usb could not read RM3100 {m.group('field').strip()}: "
                           f"{m.group('reason')} (is the chip at 0x{addr:02X}? "
                           f"try `mag-usb -O {device} -S` to scan the bus)",
            })
            continue
        m = _CHIP_UNAVAILABLE_RE.match(line)
        if m:
            out.append({
                "severity": "warn",
                "instance": _INSTANCE,
                "message": f"chip-readback unavailable: {m.group('reason')} "
                           f"(adapter at {device} not plugged in / wrong device path?)",
            })
            continue
    return out
