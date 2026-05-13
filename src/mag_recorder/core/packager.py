"""Daily JSONL -> zip rollup for PSWS upload.

The supervisor writes one ``samples-YYYY-MM-DD.jsonl`` per UTC day in
the spool dir.  When a UTC day closes, this module bundles that
file into ``OBS<YYYY-MM-DD>T00:00.zip`` in the upload-queue dir,
where the hs-uploader FileTreeSource + PswsMagnetometerSftp pipeline
picks it up.

Why one zip per day rather than one zip per "obs window":
  - The PSWS magnetometer ingest convention (per the spec we got
    from the user) is one zip per UTC day named OBS<date>T00:00.zip.
  - The "T00:00" suffix is the start-of-day in the dataset name even
    though the data spans 00:00..23:59.  This matches Grape's
    OBS<date>T<HH>-<MM> naming where T00-00 likewise marks the
    start-of-window, not a sample at that instant.

Why colons in the zip filename: literal user spec.  Trigger-directory
names use dashes (filesystem-safe); see PswsMagnetometerSftp.
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


_ZIP_NAME = "OBS{date}T00:00.zip"
_SAMPLES_NAME = "samples-{date}.jsonl"


@dataclass(frozen=True)
class PackageResult:
    """One package_day() outcome."""
    src_jsonl:   Path
    out_zip:     Path
    sample_lines: int


def yesterday_utc(now: Optional[datetime] = None) -> str:
    """``YYYY-MM-DD`` for the UTC day before ``now`` (default: real now)."""
    t = now or datetime.now(tz=timezone.utc)
    return (t - timedelta(days=1)).strftime("%Y-%m-%d")


def src_jsonl_path(spool_dir: Path, date_str: str) -> Path:
    return spool_dir / _SAMPLES_NAME.format(date=date_str)


def out_zip_path(queue_dir: Path, date_str: str) -> Path:
    return queue_dir / _ZIP_NAME.format(date=date_str)


def package_day(
    spool_dir: Path,
    queue_dir: Path,
    date_str: str,
    *,
    delete_source: bool = False,
    overwrite: bool = False,
) -> Optional[PackageResult]:
    """Zip ``samples-<date_str>.jsonl`` into ``OBS<date_str>T00:00.zip``.

    Returns the result, or ``None`` if the source file doesn't exist
    (nothing to package).  Raises ``FileExistsError`` if the target
    zip already exists and ``overwrite=False``.

    ``delete_source`` is opt-in; the safe default is to leave the
    JSONL in place so an operator can re-package or inspect it.  The
    eventual cleanup happens out-of-band (e.g. via ``smd storage
    trim``).
    """
    spool_dir = Path(spool_dir)
    queue_dir = Path(queue_dir)
    src = src_jsonl_path(spool_dir, date_str)
    if not src.is_file():
        logger.info("packager: no JSONL for %s at %s", date_str, src)
        return None

    out = out_zip_path(queue_dir, date_str)
    if out.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing zip: {out}")

    queue_dir.mkdir(parents=True, exist_ok=True)

    # Atomic write: zip into a sibling .part, then rename.  Half-written
    # zips would otherwise be picked up by the FileTreeSource on the
    # next poll.  Same .part-then-rename pattern PswsMagnetometerSftp
    # uses on the wire.
    tmp = out.with_suffix(out.suffix + ".part")
    sample_count = 0
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            # arcname keeps the JSONL filename, not the full spool
            # path, so the recipient gets samples-YYYY-MM-DD.jsonl.
            zf.write(src, arcname=src.name)
        # Count lines for the audit log; cheap, doesn't change perf
        # path because the zip is written first.
        with open(src, "rb") as fh:
            sample_count = sum(1 for _ in fh)
        tmp.replace(out)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise

    if delete_source:
        src.unlink(missing_ok=True)

    logger.info(
        "packager: %s -> %s (%d samples)", src.name, out.name, sample_count,
    )
    return PackageResult(src_jsonl=src, out_zip=out, sample_lines=sample_count)
