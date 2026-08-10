"""Timing-provenance sidecar: ``timing-YYYY-MM-DD.jsonl`` beside the spool.

mag-recorder is a HOST-CLOCK instrument: the RM3100 is not radiod-sampled,
so samples are stamped from the system clock (see supervisor._restamp) and
the RTP labelling invariant does not apply.  This sidecar records what that
clock was disciplined by — ``hamsci_dsp.timing.sysclock_timing_authority()``
(chronyc tracking: reference, stratum, leap, worst-case error bound) — so
magnetometer products meet the same annotated-timing bar as the RTP-frame
clients (hf-timestd HF_TIMESTD_SPLIT_DESIGN.md §11).

Write policy: poll every ``interval_sec`` (default 60 s, i.e. every 60th
sample at 1 Hz), append a line only when the STABLE identity of the block
changes (reference / tier / stratum / leap — not the ns-jitter fields), plus
an unconditional heartbeat line every ``heartbeat_sec`` so consumers can
bound staleness.  ~150 lines/day worst case; typically a handful.

The packager bundles the day's sidecar into the OBS zip next to the samples
file.  Every path in here is best-effort: a sidecar failure must never take
down the recorder, so nothing raises.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

try:  # hamsci-dsp >= 0.4.0; degrade to an explicit fallback block without it
    from hamsci_dsp.timing import sysclock_timing_authority as _default_block_fn
except ImportError:  # pragma: no cover - exercised only on unprovisioned hosts
    logger.warning(
        "hamsci_dsp not importable - timing sidecar will record fallback blocks"
    )
    def _default_block_fn() -> dict:
        return {"source": "sysclock-fallback", "timing_source": None,
                "t_level_active": None, "stratum": None, "leap_status": None,
                "sigma_ns": None}

# A write is triggered by a change in WHAT disciplines the clock, not by
# normal ns-level jitter of the discipline quality.
_STABLE_KEYS = ("source", "timing_source", "t_level_active", "stratum",
                "leap_status")


class TimingSidecar:
    """Append provenance lines to ``<spool_dir>/timing-YYYY-MM-DD.jsonl``."""

    def __init__(
        self,
        spool_dir: Path,
        *,
        interval_sec: float = 60.0,
        heartbeat_sec: float = 600.0,
        block_fn: Callable[[], dict] = _default_block_fn,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._dir = Path(spool_dir)
        self._interval = float(interval_sec)
        self._heartbeat = float(heartbeat_sec)
        self._block_fn = block_fn
        self._now = now_fn
        self._last_poll: Optional[float] = None
        self._last_write: Optional[float] = None
        self._last_stable: Optional[tuple] = None

    def maybe_sample(self, ts_iso: str) -> None:
        """Called once per spooled sample; polls/writes on its schedule.

        ``ts_iso`` is the sample's spool timestamp — it names the sidecar
        day file and stamps the line, so sidecar and samples join on the
        same clock.  Never raises.
        """
        try:
            now = self._now()
            if self._last_poll is not None and now - self._last_poll < self._interval:
                return
            self._last_poll = now

            block = self._block_fn()
            stable = tuple(block.get(k) for k in _STABLE_KEYS)
            changed = stable != self._last_stable
            heartbeat_due = (
                self._last_write is None
                or now - self._last_write >= self._heartbeat
            )
            if not changed and not heartbeat_due:
                return

            line = {"ts": ts_iso, **block}
            path = self._dir / f"timing-{ts_iso[:10]}.jsonl"
            self._dir.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, separators=(",", ":")) + "\n")
            self._last_stable = stable
            self._last_write = now
            if changed:
                logger.info("timing sidecar: %s / %s (stratum %s, leap %s)",
                            block.get("timing_source"),
                            block.get("t_level_active"),
                            block.get("stratum"), block.get("leap_status"))
        except Exception:  # noqa: BLE001 - sidecar must never kill the recorder
            logger.exception("timing sidecar: sample failed (continuing)")


def timing_jsonl_path(spool_dir: Path, date_str: str) -> Path:
    """Sidecar file for a UTC day (packager uses this)."""
    return Path(spool_dir) / f"timing-{date_str}.jsonl"
