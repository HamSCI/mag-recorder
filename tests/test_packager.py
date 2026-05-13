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

    # The zip contains the JSONL under its original name.
    with zipfile.ZipFile(result.out_zip, "r") as zf:
        names = zf.namelist()
        assert names == ["samples-2026-05-12.jsonl"]
        body = zf.read("samples-2026-05-12.jsonl").decode()
        assert body.count("\n") == 5
        assert '"ts":"2026-05-12T00:00:00.000Z"' in body


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
        assert "samples-2026-05-12.jsonl" in zf.namelist()


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
        '[station]\npsws_station_id = "S000082"\ninstrument_id = "RM3100"\n'
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
