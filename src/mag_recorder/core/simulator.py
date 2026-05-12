"""Synthetic mag-usb JSONL stream for development without hardware.

Generates 1 Hz lines that match the upstream mag-usb output format
(``{"ts": "DD Mon YYYY HH:MM:SS", "rt": ..., "x": ..., "y": ..., "z": ...}``)
with realistic baselines + Gaussian noise.  The supervisor consumes
the same iterator regardless of whether the source is this simulator
or a real mag-usb subprocess, so the upper layers stay source-agnostic.

Why baseline values at all: a flat-zero stream would mask bugs in the
unit handling (nT vs µT, sign extension) that a plausibly-shaped
stream would catch.  21500/1500/47500 nT is a rough mid-latitude
northern-hemisphere reference; values just have to look right, not
match a specific station.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Optional


@dataclass(frozen=True)
class SimulatorConfig:
    baseline_x_nt: float = 21500.0
    baseline_y_nt: float =  1500.0
    baseline_z_nt: float = 47500.0
    noise_nt:      float =     0.5
    baseline_rt_c: float =    22.0
    noise_rt_c:    float =     0.1
    sample_hz:     int   =     1
    seed:          Optional[int] = None


def _upstream_mag_usb_timestamp(t: datetime) -> str:
    """Format like upstream mag-usb's `%d %b %Y %T` (`27 Oct 2025 14:02:33`).

    We mimic the upstream format so the simulator output is
    indistinguishable from a real mag-usb pipe at the supervisor's
    input boundary.  The supervisor's own re-stamping logic does the
    ISO-8601-ms conversion downstream.
    """
    return t.strftime("%d %b %Y %H:%M:%S")


def generate_line(cfg: SimulatorConfig, rng: random.Random,
                  t: Optional[datetime] = None) -> dict:
    """One synthetic sample.  Returns the dict; the iterator wraps it as JSON."""
    if t is None:
        t = datetime.now(timezone.utc)
    return {
        "ts": _upstream_mag_usb_timestamp(t),
        "rt": round(cfg.baseline_rt_c + rng.gauss(0.0, cfg.noise_rt_c), 3),
        "x":  round(cfg.baseline_x_nt + rng.gauss(0.0, cfg.noise_nt), 3),
        "y":  round(cfg.baseline_y_nt + rng.gauss(0.0, cfg.noise_nt), 3),
        "z":  round(cfg.baseline_z_nt + rng.gauss(0.0, cfg.noise_nt), 3),
    }


def iter_samples(cfg: SimulatorConfig, *,
                 count: Optional[int] = None,
                 sleep: bool = True) -> Iterator[dict]:
    """Infinite (or bounded) 1 Hz sample stream.

    When ``sleep=False`` (the test path) lines come out as fast as the
    consumer pulls; when ``sleep=True`` (the daemon path) the iterator
    paces itself on a CLOCK_REALTIME-aligned deadline so the simulator
    cadence mirrors what the upstream mag-usb cadence-fix delivers.
    """
    rng = random.Random(cfg.seed)
    yielded = 0
    period = 1.0 / max(cfg.sample_hz, 1)

    if sleep:
        # Anchor on the next whole second, same shape as the C-side
        # clock_nanosleep(TIMER_ABSTIME) fix.
        deadline = float(int(time.time()) + 1)
    else:
        deadline = time.time()

    while count is None or yielded < count:
        if sleep:
            remaining = deadline - time.time()
            if remaining > 0:
                time.sleep(remaining)

        sample_t = datetime.fromtimestamp(deadline, tz=timezone.utc)
        yield generate_line(cfg, rng, t=sample_t)
        yielded += 1
        deadline += period
