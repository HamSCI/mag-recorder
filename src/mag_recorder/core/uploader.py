"""Drain the upload queue through the PSWS-magnetometer transport.

The hs-uploader library has a fully-featured Pipeline / Uploader
abstraction with watermarks and exponential backoff; mag-recorder
v0.1 wires a simpler one-shot drain:

  - Walk ``<queue_dir>/OBS*.zip`` (oldest first by mtime).
  - For each, build a single-record batch and call
    ``PswsMagnetometerSftp.ship(batch, identity)``.
  - On acked, delete the local zip.
  - On retry_later or permanent, log and stop (the next invocation
    will pick up where this one left off; permanent failures sit in
    the queue until an operator inspects them).

The full Uploader/WatermarkStore path is overkill for one zip per
day: we don't need a per-record cursor, retries are handled by the
daily systemd timer that re-runs ``mag-recorder upload``, and
dead-letter "inspection" means an operator listing the queue dir.
This drains-the-queue function can be swapped for the full Pipeline
later without changing the CLI surface.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from hs_uploader.config import StationIdentity
from hs_uploader.core import Outcome, Record, RecordBatch
from hs_uploader.transports.psws_magnetometer import (
    PswsMagnetometerSftp,
    TABLE,
)

logger = logging.getLogger(__name__)


def find_zips(queue_dir: Path) -> list[Path]:
    """Daily zips waiting to upload, oldest-mtime first."""
    if not queue_dir.exists():
        return []
    zips = sorted(
        (p for p in queue_dir.glob("OBS*.zip") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
    )
    return zips


def identity_from_config(config: dict) -> StationIdentity:
    """Build a StationIdentity from mag-recorder's TOML.

    Mirrors hf-timestd: the SSH key path and PSWS station id live in
    the consuming client's config, not in hs-uploader's, because
    different clients on the same station may share or override
    either field.
    """
    st = config.get("station", {})
    up = config.get("uploader", {})
    return StationIdentity(
        call         = st.get("callsign", ""),
        grid         = st.get("grid_square", ""),
        station_id   = st.get("psws_station_id", ""),
        ssh_key_file = up.get("ssh_key_file", ""),
    )


def transport_from_config(
    config: dict,
    *,
    dry_run: bool = False,
) -> PswsMagnetometerSftp:
    """Construct the transport from the [uploader] + [station] blocks."""
    st = config.get("station", {})
    up = config.get("uploader", {})
    return PswsMagnetometerSftp(
        instrument_id        = st.get("instrument_id", "RM3100"),
        host                 = up.get("host", "pswsnetwork.eng.ua.edu"),
        sftp_user            = up.get("user") or None,
        ssh_key_file         = up.get("ssh_key_file") or None,
        bandwidth_limit_kbps = up.get("bandwidth_limit_kbps"),
        dry_run              = dry_run,
    )


def drain_queue(
    queue_dir: Path,
    config: dict,
    *,
    dry_run: bool = False,
    max_uploads: Optional[int] = None,
) -> tuple[int, int, list[Path]]:
    """Ship every queued zip; return (acked, failed, remaining_paths).

    Stops on the first non-acked outcome to avoid stampeding the
    PSWS server when it's down.  ``remaining_paths`` lets the caller
    decide what to do (typically: nothing — the next scheduled run
    will retry).
    """
    zips = find_zips(queue_dir)
    if not zips:
        return 0, 0, []

    transport = transport_from_config(config, dry_run=dry_run)
    identity = identity_from_config(config)
    acked = 0
    failed = 0
    remaining: list[Path] = []

    for i, zip_path in enumerate(zips):
        if max_uploads is not None and i >= max_uploads:
            remaining.append(zip_path)
            continue
        rec = Record(
            table=TABLE,
            time=datetime.now(tz=timezone.utc),
            columns={},
            payload_path=zip_path,
        )
        batch = RecordBatch(records=[rec], cursor_after=b"")
        outcome = transport.ship(batch, identity)

        if outcome.kind == "acked":
            acked += 1
            if not dry_run:
                # Source-side delete-on-ack semantics, inlined.
                try:
                    zip_path.unlink()
                    logger.info("uploader: deleted %s after ack", zip_path.name)
                except OSError as exc:
                    logger.warning("uploader: could not delete %s: %s",
                                   zip_path, exc)
        else:
            failed += 1
            logger.warning(
                "uploader: %s for %s (%s); stopping queue drain",
                outcome.kind, zip_path.name, outcome.reason,
            )
            # Everything from this zip onward stays queued.
            remaining.extend(zips[i:])
            break

    return acked, failed, remaining
