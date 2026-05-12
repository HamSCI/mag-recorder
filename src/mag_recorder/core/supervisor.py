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


def _mag_usb_source(binary: str, device: str,
                    config_path: Optional[str] = None) -> Iterator[dict]:
    """Spawn mag-usb, parse JSONL lines from stdout.

    Lines that don't start with `{` go to stderr (mag-usb mixes its
    own diagnostic messages with JSON lines on the same FD;
    docs/Data-Format.md acknowledges this).  We log the rejects at
    DEBUG so they don't get lost but don't pollute the spool.
    """
    cmd = [binary, "-O", device]
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
    return _mag_usb_source(
        binary       = mag_cfg.get("mag_usb_binary", "/usr/local/bin/mag-usb"),
        device       = mag_cfg.get("device",         "/dev/ttyMAG0"),
        config_path  = mag_cfg.get("mag_usb_config", "/etc/mag-usb/config.toml"),
    )
