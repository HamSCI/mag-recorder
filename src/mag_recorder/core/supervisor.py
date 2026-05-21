"""Top-level supervisor: consume JSONL, re-stamp, persist.

Two input sources, behind one iterator interface:
  - ``SimulatorSource``    : synthetic, no hardware needed (development).
  - ``MagUsbSubprocessSource``  : spawns ``mag-usb`` and reads its stdout.

The supervisor doesn't care which one it has.  It re-stamps each
incoming sample with a millisecond-precision ISO-8601 UTC timestamp
(upstream mag-usb emits second-resolution; we lose nothing by
re-stamping at receive time given the cadence is wall-clock aligned),
appends to a daily JSONL spool, and pings the systemd watchdog.

What's deliberately NOT here yet:
  - Daily zip packaging (depends on the hs-uploader PSWS-mag transport
    landing; see project_mag_recorder.md memory for the spec).
  - sigmond authority-driven timestamp source (TIMING-PIPELINE-WIRING
    Pattern B is not generally available across clients yet; until it
    is, ``datetime.now(UTC)`` is the right thing to use).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from mag_recorder.core.simulator import SimulatorConfig, iter_samples

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def _simulator_source(cfg: dict) -> Iterator[dict]:
    sim = SimulatorConfig(
        baseline_x_nt = float(cfg.get("baseline_x_nt", 21500.0)),
        baseline_y_nt = float(cfg.get("baseline_y_nt",  1500.0)),
        baseline_z_nt = float(cfg.get("baseline_z_nt", 47500.0)),
        noise_nt      = float(cfg.get("noise_nt",          0.5)),
        baseline_rt_c = float(cfg.get("baseline_rt_c",    22.0)),
        noise_rt_c    = float(cfg.get("noise_rt_c",        0.1)),
        sample_hz     = int(cfg.get("sample_hz", 1)),
    )
    yield from iter_samples(sim, sleep=True)


def build_mag_usb_argv(binary: str, device: str, i2c_address: int,
                       driver_config_path: str,
                       websocket: Optional[dict] = None) -> list[str]:
    """Construct the argv mag-usb is spawned with.

    Factored out of ``_mag_usb_source`` for unit-testability (we can
    assert the argv shape without spawning a subprocess).  The
    contract:

    - ``-O <device>``  -- adapter path; matches install/99-PololuI2C.rules's
                          /dev/ttyMAG0 symlink by default.
    - ``-f <path>``    -- explicit config file (wittend/mag-usb sigmond-integration
                          PR #2).  When -f is given mag-usb skips the historical
                          auto-discovery of /etc/mag-usb/config.toml and
                          ./config.toml, and a missing file is a hard error.
                          mag-recorder renders this file fresh on every daemon
                          start via mag_recorder.core.driver_config.render().
    - ``-A 0x<addr>``  -- I2C address override (same PR).  Cheap belt-and-braces:
                          even though the rendered driver TOML also sets the
                          address, passing -A here means an out-of-sync TOML
                          can't silently route us to the wrong device.
    - ``-W -w -a``     -- optional WebSocket server (existing).
    """
    cmd = [binary,
           "-O", device,
           "-f", driver_config_path,
           "-A", f"0x{int(i2c_address):02x}"]
    ws = websocket or {}
    if ws.get("enable"):
        cmd += ["-W",
                "-w", str(int(ws.get("port", 8765))),
                "-a", str(ws.get("bind_address", "0.0.0.0"))]
    return cmd


def _mag_usb_source(binary: str, device: str, i2c_address: int,
                    driver_config_path: str,
                    websocket: Optional[dict] = None) -> Iterator[dict]:
    """Spawn mag-usb, parse JSONL lines from stdout.

    Lines that don't start with `{` go to stderr (mag-usb mixes its
    own diagnostic messages with JSON lines on the same FD;
    docs/Data-Format.md acknowledges this).  We log the rejects at
    DEBUG so they don't get lost but don't pollute the spool.
    """
    cmd = build_mag_usb_argv(
        binary=binary, device=device, i2c_address=i2c_address,
        driver_config_path=driver_config_path, websocket=websocket,
    )
    logger.info("spawning upstream mag-usb: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # line-buffered
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                logger.debug("mag-usb non-JSON: %s", line)
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("mag-usb bad JSON (%s): %s", e, line)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# ---------------------------------------------------------------------------
# Sink
# ---------------------------------------------------------------------------

class DailySpoolWriter:
    """Append JSONL to ``<spool_dir>/samples-YYYY-MM-DD.jsonl``, rotating at UTC midnight."""

    def __init__(self, spool_dir: Path) -> None:
        self._dir = spool_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh: Optional[object] = None
        self._date: Optional[str] = None

    def _path_for(self, date_str: str) -> Path:
        return self._dir / f"samples-{date_str}.jsonl"

    def write(self, sample: dict) -> None:
        """Append one JSON-encoded line; rotate on UTC date change."""
        date_str = sample["ts"][:10]  # ISO-8601 "YYYY-MM-DD"
        line = json.dumps(sample, separators=(",", ":")) + "\n"
        with self._lock:
            if self._date != date_str:
                if self._fh is not None:
                    self._fh.close()  # type: ignore[union-attr]
                self._fh = open(self._path_for(date_str), "a", encoding="utf-8")
                self._date = date_str
            self._fh.write(line)  # type: ignore[union-attr]
            self._fh.flush()  # type: ignore[union-attr]

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()  # type: ignore[union-attr]
                self._fh = None
                self._date = None


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------

def _restamp(sample: dict) -> dict:
    """Replace the upstream `ts` with ISO-8601 UTC at ms precision.

    Upstream mag-usb emits `"ts": "DD Mon YYYY HH:MM:SS"` (no
    timezone, no fractional second).  We re-stamp at receive time
    because the new cadence-aligned loop guarantees we receive the
    line within a few ms of its intended deadline — close enough for
    1 Hz geomagnetic data and a strict upgrade over the stringified
    upstream timestamp.  Sub-second alignment work belongs upstream
    or in a sigmond authority feed; not here.
    """
    sample = dict(sample)
    now = datetime.now(timezone.utc)
    # `2026-05-12T23:45:01.123Z`  -- explicit Z suffix beats "+00:00".
    sample["ts"] = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    return sample


@dataclass
class SupervisorConfig:
    spool_dir:    Path
    source:       Iterable[dict]
    watchdog_ping: Optional[Callable[[], None]] = None  # sd_notify, or None


def run_supervisor(cfg: SupervisorConfig, *, stop_event: Optional[threading.Event] = None) -> None:
    """Drive the source -> restamp -> spool pipeline.

    ``stop_event``, when set, ends the loop after the next sample.
    Tests pass an Event; the daemon CLI installs SIGTERM/SIGINT
    handlers that set the same flag.
    """
    writer = DailySpoolWriter(cfg.spool_dir)
    try:
        for sample in cfg.source:
            if stop_event is not None and stop_event.is_set():
                logger.info("stop requested; exiting supervisor")
                break
            try:
                restamped = _restamp(sample)
                writer.write(restamped)
                if cfg.watchdog_ping is not None:
                    cfg.watchdog_ping()
            except Exception:
                logger.exception("supervisor: error processing sample %r", sample)
    finally:
        writer.close()


def make_source(config: dict, *, force_simulate: bool = False) -> Iterator[dict]:
    """Pick the right source per config + CLI overrides."""
    sim_cfg = config.get("simulator", {})
    if force_simulate or sim_cfg.get("enabled"):
        logger.info("using simulator source (no hardware required)")
        return _simulator_source(sim_cfg)

    mag_cfg = config.get("mag", {})

    # Materialize a fresh mag-usb driver TOML from this run's loaded
    # config -- see core/driver_config.py for the schema mapping.  The
    # path defaults to /run/mag-recorder/<basename>, which systemd
    # creates+chowns via RuntimeDirectory=mag-recorder in the unit
    # file; setting [mag].driver_config_path overrides for tests /
    # ad-hoc runs.
    from .driver_config import render as _render_driver_config
    driver_config_path = mag_cfg.get(
        "driver_config_path",
        "/run/mag-recorder/mag-usb-driver.toml",
    )
    _render_driver_config(config, driver_config_path)
    logger.info("rendered mag-usb driver config: %s", driver_config_path)

    return _mag_usb_source(
        binary             = mag_cfg.get("mag_usb_binary", "/usr/local/bin/mag-usb"),
        device             = mag_cfg.get("device",         "/dev/ttyMAG0"),
        i2c_address        = int(mag_cfg.get("i2c_address", 0x23)),
        driver_config_path = driver_config_path,
        websocket          = config.get("websocket", {}),
    )
