"""Daily JSONL -> zip rollup for PSWS upload.

The supervisor writes one ``samples-YYYY-MM-DD.jsonl`` per UTC day in
the spool dir.  When a UTC day closes, this module bundles that day
into ``OBS<YYYY-MM-DD>T00:00.zip`` in the upload-queue dir, where the
hs-uploader FileTreeSource + PswsMagnetometerSftp pipeline (or the
``mag-recorder upload`` timer) picks it up.

PAYLOAD CONVENTION (verified against zips PSWS actually ingested,
2026-08-21 — stations AB4EJ-m and N7FWL): the zip holds exactly ONE file
named ``<site>-<YYYYMMDD>-runmag.log`` whose lines are mag-usb / runMag
NATIVE samples::

    { "ts":"21 Aug 2026 00:00:01", "rt":23.31, "x":-50.181, "y":-4.442, "z":15.945 }

``ts`` is UTC at second resolution in runMag's ``DD Mon YYYY HH:MM:SS``
form; ``rt`` is the remote sensor temperature (°C); ``x``/``y``/``z`` are
nT.  Our local spool keeps its own JSONL shape (ISO ``ts`` with ms,
``reporter_id``); the packager CONVERTS on the way out and drops any
line that is not a complete sample.  Four days of the previous payload
(``samples-<date>.jsonl`` + ``timing-<date>.jsonl``) were stored by PSWS
but never ingested (S000170 / instrument 372) — the ingester keys on the
runMag log convention, not on the zip name.  The timing-provenance
sidecar therefore stays OUT of the PSWS zip.

Why one zip per day named OBS<date>T00:00.zip: that is the name PSWS
lists for magnetometer observations (colons included); "T00:00" marks
the start of the UTC day, matching Grape's OBS<date>T00-00 directories.
Trigger-directory names use dashes (filesystem-safe); see
PswsMagnetometerSftp.
"""

from __future__ import annotations

import json
import logging
import math
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


_ZIP_NAME = "OBS{date}T00:00.zip"
_SAMPLES_NAME = "samples-{date}.jsonl"
_LOG_NAME = "{site}-{ymd}-runmag.log"
_DEFAULT_SITE = "OBS"
_SITE_BAD = re.compile(r"[^A-Za-z0-9_-]")


def site_token(site: Optional[str]) -> str:
    """The ``<site>`` prefix of the runMag log name: callsign-ish, never a
    path.  runMag forbids ``/ ' " *`` etc. in its site prefix; anything
    outside ``[A-Za-z0-9_-]`` becomes ``_`` ("AC0G/B4" -> "AC0G_B4").
    Empty/None falls back to a neutral prefix rather than refusing to
    ship a day — PSWS keys on the ``-runmag.log`` suffix.

    ⛔ A TEMPLATE value is not an identity either.  The sanitiser is
    indiscriminate, so an unedited ``"<YOUR_CALL>"`` would sanitise to
    ``_YOUR_CALL_`` and bake it into a shipped artifact — an archive
    asserting a callsign nobody holds.  Treat it as absent and take the
    neutral prefix, which is exactly what the empty case already does."""
    from mag_recorder.config import is_placeholder
    if is_placeholder(site):
        return _DEFAULT_SITE
    tok = _SITE_BAD.sub("_", (site or "").strip())
    return tok or _DEFAULT_SITE


def to_runmag_line(jsonl_line: str) -> Optional[str]:
    """Convert one local spool line to the runMag/mag-usb native line PSWS
    ingests, or ``None`` if the line is not a complete sample (dropped)."""
    try:
        d = json.loads(jsonl_line)
        ts = datetime.strptime(str(d["ts"])[:19], "%Y-%m-%dT%H:%M:%S")
        rt = float(d["rt"]); x = float(d["x"]); y = float(d["y"]); z = float(d["z"])
    except (ValueError, KeyError, TypeError):
        return None
    return (f'{{ "ts":"{ts.strftime("%d %b %Y %H:%M:%S")}", "rt":{rt:.2f}, '
            f'"x":{x:.3f}, "y":{y:.3f}, "z":{z:.3f} }}')


@dataclass(frozen=True)
class PackageResult:
    """One package_day() outcome."""
    src_jsonl:   Path
    out_zip:     Path
    sample_lines: int


@dataclass(frozen=True)
class PackageSkipped:
    """package_day() outcome for a day whose samples all read zero.

    Nothing was built.  ``complete`` counts samples with all of x/y/z/rt
    present (all zero in x/y/z); ``missing`` counts lines without a usable
    value (null, NaN, absent).  ``first_report`` is False when an earlier
    run already logged this skip."""
    src_jsonl:    Path
    date:         str
    complete:     int
    missing:      int
    first_report: bool
    skipped:      bool = True


def _field(d: dict, key: str) -> Optional[float]:
    """A finite float, or None for a missing/NaN/non-numeric value."""
    try:
        v = float(d[key])
    except (ValueError, KeyError, TypeError):
        return None
    return v if math.isfinite(v) else None


def scan_day(src: Path) -> tuple[int, int, int]:
    """(complete, missing, nonzero) over the day's spool lines.

    ``nonzero`` counts samples with a finite, non-zero x, y or z.  A
    missing or NaN value is not a zero and not a reading: it never makes a
    day non-zero.  A sensor that NACKs records zeros, so a day with only
    zeros and missing samples carries no measurement."""
    complete = missing = nonzero = 0
    with open(src, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                d = json.loads(raw)
                vals = [_field(d, k) for k in ("x", "y", "z")]
            except (ValueError, TypeError, AttributeError):
                missing += 1
                continue
            if all(v is None for v in vals):
                missing += 1
                continue
            complete += 1
            if any(v for v in vals if v is not None):
                nonzero += 1
    return complete, missing, nonzero


def _zero_marker(spool_dir: Path, date_str: str) -> Path:
    return spool_dir / f"samples-{date_str}.allzero"


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
    site: Optional[str] = None,
) -> Optional[PackageResult]:
    """Zip ``samples-<date_str>.jsonl`` into ``OBS<date_str>T00:00.zip`` as
    a single ``<site>-<YYYYMMDD>-runmag.log`` of native runMag lines (see
    module docstring).  ``sample_lines`` counts the samples SHIPPED.

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

    # All-zero day: a sensor that NACKs records zeros.  Build nothing.  The
    # marker holds the source size, so the next run stays quiet unless the
    # day has since grown (a day that gains real samples is re-evaluated).
    complete, missing, nonzero = scan_day(src)
    marker = _zero_marker(spool_dir, date_str)
    if nonzero == 0:
        size = str(src.stat().st_size)
        try:
            seen = marker.read_text().strip() == size
        except OSError:
            seen = False
        if not seen:
            logger.warning(
                "packager: %s: skipped, all-zero day (%d zero sample(s), "
                "%d missing); no zip built", date_str, complete, missing)
            try:
                marker.write_text(size + "\n")
            except OSError as exc:
                logger.warning("packager: cannot write %s: %s", marker, exc)
        return PackageSkipped(src_jsonl=src, date=date_str, complete=complete,
                              missing=missing, first_report=not seen)
    marker.unlink(missing_ok=True)

    queue_dir.mkdir(parents=True, exist_ok=True)

    # Atomic write: zip into a sibling .part, then rename.  Half-written
    # zips would otherwise be picked up by the FileTreeSource on the
    # next poll.  Same .part-then-rename pattern PswsMagnetometerSftp
    # uses on the wire.
    tmp = out.with_suffix(out.suffix + ".part")
    sample_count = 0
    dropped = 0
    log_name = _LOG_NAME.format(site=site_token(site),
                                ymd=date_str.replace("-", ""))
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf, \
             zf.open(log_name, "w") as out_fh, \
             open(src, "r", encoding="utf-8", errors="replace") as in_fh:
            for raw in in_fh:
                raw = raw.strip()
                if not raw:
                    continue
                line = to_runmag_line(raw)
                if line is None:
                    dropped += 1
                    continue
                out_fh.write((line + "\n").encode("utf-8"))
                sample_count += 1
        tmp.replace(out)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
    if dropped:
        logger.warning("packager: %s: dropped %d non-sample line(s)",
                       src.name, dropped)

    if delete_source:
        src.unlink(missing_ok=True)

    logger.info(
        "packager: %s -> %s [%s] (%d samples)",
        src.name, out.name, log_name, sample_count,
    )
    return PackageResult(src_jsonl=src, out_zip=out, sample_lines=sample_count)
