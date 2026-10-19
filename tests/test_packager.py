"""packager.py tests — daily JSONL -> OBS<date>T00:00.zip."""

from __future__ import annotations

import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mag_recorder.core.packager import (
    PackageResult,
    out_zip_path,
    package_day,
    src_jsonl_path,
    yesterday_utc,
)


def _write_jsonl(path: Path, n_samples: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f'{{"ts":"2026-05-12T{h:02d}:00:00.000Z","x":{h*10}.0,"y":1.0,"z":2.0,"rt":22.0}}\n'
        for h in range(n_samples)
    ]
    path.write_text("".join(lines))


def test_yesterday_utc_returns_date_before_now():
    now = datetime(2026, 5, 13, 0, 0, 1, tzinfo=timezone.utc)
    assert yesterday_utc(now) == "2026-05-12"


def test_yesterday_utc_handles_month_boundary():
    now = datetime(2026, 6, 1, 0, 0, 1, tzinfo=timezone.utc)
    assert yesterday_utc(now) == "2026-05-31"


def test_path_helpers(tmp_path: Path):
    spool = tmp_path / "spool"
    queue = tmp_path / "queue"
    assert src_jsonl_path(spool, "2026-05-12") == spool / "samples-2026-05-12.jsonl"
    assert out_zip_path(queue, "2026-05-12") == queue / "OBS2026-05-12T00:00.zip"


def test_package_day_creates_zip_with_jsonl(tmp_path: Path):
    spool = tmp_path / "spool"
    queue = tmp_path / "queue"
    src = spool / "samples-2026-05-12.jsonl"
    _write_jsonl(src, n_samples=5)

    result = package_day(spool, queue, "2026-05-12")

    assert isinstance(result, PackageResult)
    assert result.out_zip == queue / "OBS2026-05-12T00:00.zip"
    assert result.sample_lines == 5
    assert result.out_zip.is_file()

    # Source JSONL is preserved by default.
    assert src.is_file()

    # The zip contains ONE runMag-convention log (PSWS ingest contract).
    with zipfile.ZipFile(result.out_zip, "r") as zf:
        names = zf.namelist()
        assert names == ["OBS-20260512-runmag.log"]
        body = zf.read("OBS-20260512-runmag.log").decode()
        assert body.count("\n") == 5
        assert '"ts":"12 May 2026 00:00:00"' in body


def test_package_day_atomic_via_part_rename(tmp_path: Path):
    """Half-written .part should not exist after a successful run."""
    spool = tmp_path / "spool"
    queue = tmp_path / "queue"
    _write_jsonl(spool / "samples-2026-05-12.jsonl")

    package_day(spool, queue, "2026-05-12")

    assert (queue / "OBS2026-05-12T00:00.zip").is_file()
    leftovers = list(queue.glob("*.part"))
    assert leftovers == []


def test_package_day_no_jsonl_returns_none(tmp_path: Path):
    spool = tmp_path / "spool"
    spool.mkdir()
    queue = tmp_path / "queue"
    result = package_day(spool, queue, "2026-05-12")
    assert result is None


def test_package_day_refuses_overwrite_by_default(tmp_path: Path):
    spool = tmp_path / "spool"
    queue = tmp_path / "queue"
    _write_jsonl(spool / "samples-2026-05-12.jsonl")
    queue.mkdir()
    (queue / "OBS2026-05-12T00:00.zip").write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        package_day(spool, queue, "2026-05-12")


def test_package_day_overwrite_when_requested(tmp_path: Path):
    spool = tmp_path / "spool"
    queue = tmp_path / "queue"
    _write_jsonl(spool / "samples-2026-05-12.jsonl", n_samples=2)
    queue.mkdir()
    (queue / "OBS2026-05-12T00:00.zip").write_bytes(b"old garbage")

    result = package_day(spool, queue, "2026-05-12", overwrite=True)
    assert result is not None
    assert result.sample_lines == 2
    # Verify it really wrote a zip, not bypassed it.
    with zipfile.ZipFile(result.out_zip) as zf:
        assert zf.namelist() == ["OBS-20260512-runmag.log"]


def test_package_day_delete_source_only_when_asked(tmp_path: Path):
    spool = tmp_path / "spool"
    queue = tmp_path / "queue"
    src = spool / "samples-2026-05-12.jsonl"
    _write_jsonl(src)

    package_day(spool, queue, "2026-05-12", delete_source=True)
    assert not src.exists()


def test_package_day_filename_has_colons(tmp_path: Path):
    """Per user spec the zip name is OBS<date>T00:00.zip (colon, not dash)."""
    spool = tmp_path / "spool"
    queue = tmp_path / "queue"
    _write_jsonl(spool / "samples-2026-05-12.jsonl")

    result = package_day(spool, queue, "2026-05-12")
    assert result.out_zip.name == "OBS2026-05-12T00:00.zip"


def test_cli_package_exits_zero_when_no_jsonl(tmp_path: Path, monkeypatch, capsys):
    """The systemd timer fires daily; an empty day must not "fail" the unit.

    Regression for mag-recorder-upload.service Step 1: previously
    `mag-recorder package` exited 1 when there was no JSONL for the
    day, which would make the systemd oneshot service fail and
    skip the upload step entirely.  Now: warn + exit 0.
    """
    import sys

    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        '[station]\npsws_station_id = "S000082"\ninstrument_id = "372"\n'
        '[paths]\n'
        f'spool_dir = "{tmp_path}/spool"\n'
        f'upload_queue_dir = "{tmp_path}/queue"\n'
        '[simulator]\nenabled = true\n'
    )
    (tmp_path / "spool").mkdir()
    (tmp_path / "queue").mkdir()

    monkeypatch.setattr(sys, "argv", [
        "mag-recorder", "package",
        "--config", str(cfg),
        "--date", "2026-05-12",
    ])

    from mag_recorder.cli import main
    # No SystemExit when there's nothing to package -- main returns
    # normally so the systemd oneshot moves on to the upload step.
    main()
    captured = capsys.readouterr()
    # The "packaged N samples" line is suppressed; the warning is on stderr.
    assert "packaged" not in captured.out


def test_package_day_excludes_timing_sidecar(tmp_path):
    """PSWS ingests a zip holding exactly one runMag log; the timing
    sidecar stays local (it was riding along in the never-ingested
    payload)."""
    import json, zipfile
    from mag_recorder.core.packager import package_day
    spool = tmp_path / "spool"; queue = tmp_path / "queue"
    spool.mkdir()
    _write_jsonl(spool / "samples-2026-08-10.jsonl", n_samples=2)
    (spool / "timing-2026-08-10.jsonl").write_text(
        json.dumps({"ts": "2026-08-10T00:00:00.000Z", "source": "chrony-sysclock"}) + "\n")
    res = package_day(spool, queue, "2026-08-10", site="AC0G")
    assert res is not None
    with zipfile.ZipFile(res.out_zip) as zf:
        assert zf.namelist() == ["AC0G-20260810-runmag.log"]
    assert (spool / "timing-2026-08-10.jsonl").is_file()   # untouched locally


def test_package_day_without_sidecar_still_packages(tmp_path):
    import zipfile
    from mag_recorder.core.packager import package_day
    spool = tmp_path / "spool"; queue = tmp_path / "queue"
    spool.mkdir()
    _write_jsonl(spool / "samples-2026-08-10.jsonl", n_samples=1)
    res = package_day(spool, queue, "2026-08-10", site="AC0G")
    with zipfile.ZipFile(res.out_zip) as zf:
        assert zf.namelist() == ["AC0G-20260810-runmag.log"]


# --- PSWS runMag-log payload convention (found 2026-08-21: PSWS ingests a zip
# only when it holds ONE file named <site>-<YYYYMMDD>-runmag.log in mag-usb /
# runMag native line format; our samples-<date>.jsonl + timing sidecar were
# stored but never ingested — S000170 instrument 372, four days unconsumed). ---

def _write_native_lines(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"ts":"2026-08-21T23:40:23.805Z","rt":27.88,"x":-40678.491,"y":1817.005,"z":-23443.356,"reporter_id":"S000170"}\n'
        '{"ts":"2026-08-21T23:40:24.805Z","rt":27.9,"x":-40677.0,"y":1817.1,"z":-23443.0,"reporter_id":"S000170"}\n'
        'this line is not json\n'
        '{"ts":"2026-08-21T23:40:25.805Z","rt":27.88,"x":-40678.2,"y":1817.0,"z":-23443.1}\n'
    )


def test_package_day_writes_single_runmag_log_named_by_site(tmp_path: Path):
    spool = tmp_path / "spool"; queue = tmp_path / "queue"
    _write_native_lines(spool / "samples-2026-08-21.jsonl")
    (spool / "timing-2026-08-21.jsonl").write_text('{"ts":"2026-08-21T00:00:00.000Z","source":"chrony"}\n')

    res = package_day(spool, queue, "2026-08-21", site="AC0G")

    with zipfile.ZipFile(res.out_zip) as zf:
        assert zf.namelist() == ["AC0G-20260821-runmag.log"]   # ONE file, sidecar excluded
        body = zf.read("AC0G-20260821-runmag.log").decode()
    lines = body.splitlines()
    assert lines[0] == '{ "ts":"21 Aug 2026 23:40:23", "rt":27.88, "x":-40678.491, "y":1817.005, "z":-23443.356 }'
    assert lines[1] == '{ "ts":"21 Aug 2026 23:40:24", "rt":27.90, "x":-40677.000, "y":1817.100, "z":-23443.000 }'
    assert len(lines) == 3            # the non-JSON line is dropped, not shipped
    assert res.sample_lines == 3      # counts shipped samples
    assert res.out_zip.name == "OBS2026-08-21T00:00.zip"   # zip name unchanged (PSWS lists it)


def test_package_day_site_defaults_to_obs_when_unknown(tmp_path: Path):
    """A site token is required by the convention; with none configured the
    packager still ships (PSWS keys on the -runmag.log suffix), using a
    neutral prefix rather than refusing a day of data."""
    spool = tmp_path / "spool"; queue = tmp_path / "queue"
    _write_native_lines(spool / "samples-2026-08-21.jsonl")
    res = package_day(spool, queue, "2026-08-21")
    with zipfile.ZipFile(res.out_zip) as zf:
        assert zf.namelist() == ["OBS-20260821-runmag.log"]


def test_package_day_site_token_is_sanitised(tmp_path: Path):
    """'AC0G/B4' must not become a path inside the zip; runMag forbids /'\"* in
    the site prefix."""
    spool = tmp_path / "spool"; queue = tmp_path / "queue"
    _write_native_lines(spool / "samples-2026-08-21.jsonl")
    res = package_day(spool, queue, "2026-08-21", site="AC0G/B4")
    with zipfile.ZipFile(res.out_zip) as zf:
        assert zf.namelist() == ["AC0G_B4-20260821-runmag.log"]


def test_to_runmag_line_formats_native_shape():
    from mag_recorder.core.packager import to_runmag_line
    assert to_runmag_line('{"ts":"2026-05-12T00:00:00.000Z","x":0.0,"y":1.0,"z":2.0,"rt":22.0}') == \
        '{ "ts":"12 May 2026 00:00:00", "rt":22.00, "x":0.000, "y":1.000, "z":2.000 }'
    assert to_runmag_line('garbage') is None
    assert to_runmag_line('{"ts":"2026-05-12T00:00:00.000Z","x":0.0}') is None   # incomplete sample dropped


# --- all-zero day: skipped, logged once, no zip (v3.73 row 3) -------------

def _write_values(path: Path, rows) -> None:
    """rows: list of (x, y, z) as JSON literals (strings), e.g. '0.0', 'null', 'NaN'."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(
        f'{{"ts":"2026-05-12T00:00:{i:02d}.000Z","x":{x},"y":{y},"z":{z},"rt":22.0}}\n'
        for i, (x, y, z) in enumerate(rows)))


def _skip_lines(caplog):
    return [r for r in caplog.records if "all-zero" in r.getMessage()]


def test_all_zero_day_is_skipped_and_logged_once(tmp_path: Path, caplog):
    import logging
    spool, queue = tmp_path / "spool", tmp_path / "queue"
    _write_values(spool / "samples-2026-05-12.jsonl", [("0.0", "0.0", "0.0")] * 4)
    with caplog.at_level(logging.INFO):
        first = package_day(spool, queue, "2026-05-12")
        second = package_day(spool, queue, "2026-05-12")
    assert not out_zip_path(queue, "2026-05-12").exists()
    assert not list(queue.glob("*")) if queue.exists() else True
    assert first is not None and getattr(first, "skipped", False)
    assert second is not None and getattr(second, "skipped", False)
    lines = _skip_lines(caplog)
    assert len(lines) == 1
    assert "2026-05-12" in lines[0].getMessage()
    assert "4" in lines[0].getMessage()


def test_mixed_day_packages(tmp_path: Path):
    spool, queue = tmp_path / "spool", tmp_path / "queue"
    _write_values(spool / "samples-2026-05-12.jsonl",
                  [("0.0", "0.0", "0.0"), ("0.0", "0.0", "0.5"), ("0.0", "0.0", "0.0")])
    result = package_day(spool, queue, "2026-05-12")
    assert not getattr(result, "skipped", False)
    assert result.sample_lines == 3
    assert result.out_zip.is_file()


def test_zeros_and_missing_samples_count_as_all_zero(tmp_path: Path, caplog):
    import logging
    spool, queue = tmp_path / "spool", tmp_path / "queue"
    _write_values(spool / "samples-2026-05-12.jsonl",
                  [("0.0", "0.0", "0.0"), ("null", "null", "null"),
                   ("NaN", "NaN", "NaN"), ("0", "0", "0")])
    with caplog.at_level(logging.INFO):
        result = package_day(spool, queue, "2026-05-12")
    assert getattr(result, "skipped", False)
    assert not out_zip_path(queue, "2026-05-12").exists()
    assert len(_skip_lines(caplog)) == 1


def test_skipped_day_that_gains_real_samples_packages(tmp_path: Path):
    spool, queue = tmp_path / "spool", tmp_path / "queue"
    src = spool / "samples-2026-05-12.jsonl"
    _write_values(src, [("0.0", "0.0", "0.0")] * 2)
    assert getattr(package_day(spool, queue, "2026-05-12"), "skipped", False)
    with open(src, "a") as fh:
        fh.write('{"ts":"2026-05-12T00:01:00.000Z","x":1.5,"y":0.0,"z":0.0,"rt":22.0}\n')
    result = package_day(spool, queue, "2026-05-12")
    assert not getattr(result, "skipped", False)
    assert result.out_zip.is_file()


def test_cli_all_zero_day_exits_zero_with_one_line(tmp_path: Path, monkeypatch, caplog):
    import logging, sys
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        '[station]\npsws_station_id = "S000082"\ninstrument_id = "372"\n'
        '[paths]\n'
        f'spool_dir = "{tmp_path}/spool"\n'
        f'upload_queue_dir = "{tmp_path}/queue"\n'
        '[simulator]\nenabled = true\n')
    _write_values(tmp_path / "spool" / "samples-2026-05-12.jsonl", [("0", "0", "0")] * 3)
    from mag_recorder.cli import main
    for _ in range(2):
        monkeypatch.setattr(sys, "argv", ["mag-recorder", "package", "--config",
                                          str(cfg), "--date", "2026-05-12"])
        with caplog.at_level(logging.INFO):
            main()
    assert not list((tmp_path / "queue").glob("*.zip"))
    msgs = [r.getMessage() for r in caplog.records]
    assert len([m for m in msgs if "all-zero" in m]) == 1
    assert not any("nothing to package" in m for m in msgs)
