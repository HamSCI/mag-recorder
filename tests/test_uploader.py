"""uploader.py tests — drain a queue of OBS zips via PswsMagnetometerSftp."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from mag_recorder.core.uploader import (
    drain_queue,
    find_zips,
    identity_from_config,
    transport_from_config,
)


def _config(tmp_path: Path, dry_run_disabled: bool = False) -> dict:
    return {
        "station": {
            "psws_station_id": "S000082",
            "instrument_id":   "RM3100",
            "callsign":        "AC0G",
            "grid_square":     "EM38ww",
        },
        "uploader": {
            "enabled":      True,
            "host":         "pswsnetwork.eng.ua.edu",
            "user":         "S000082",
            "ssh_key_file": "/etc/hs-uploader/keys/id_ed25519",
            "bandwidth_limit_kbps": 100,
        },
        "paths": {
            "spool_dir":        str(tmp_path / "spool"),
            "upload_queue_dir": str(tmp_path / "queue"),
        },
    }


def _make_queue(tmp_path: Path, dates: list[str]) -> Path:
    queue = tmp_path / "queue"
    queue.mkdir()
    now = time.time()
    for i, date in enumerate(dates):
        z = queue / f"OBS{date}T00:00.zip"
        z.write_bytes(b"PK\x03\x04 ... fake zip ...")
        # Stagger mtimes so find_zips ordering is deterministic.
        import os
        os.utime(z, (now + i, now + i))
    return queue


# ---- helpers -----------------------------------------------------------------


def test_find_zips_returns_empty_on_missing_dir(tmp_path: Path):
    assert find_zips(tmp_path / "does-not-exist") == []


def test_find_zips_ignores_non_obs_files(tmp_path: Path):
    queue = tmp_path / "queue"
    queue.mkdir()
    (queue / "OBS2026-05-12T00:00.zip").write_bytes(b"x")
    (queue / "stray.txt").write_bytes(b"x")
    (queue / "samples.jsonl").write_bytes(b"x")
    found = find_zips(queue)
    assert [p.name for p in found] == ["OBS2026-05-12T00:00.zip"]


def test_find_zips_orders_oldest_first(tmp_path: Path):
    queue = _make_queue(tmp_path, ["2026-05-10", "2026-05-12", "2026-05-11"])
    # Files were written in dates order, mtime stamped sequentially,
    # so find_zips should return them in that creation order.
    found = find_zips(queue)
    assert [p.name for p in found] == [
        "OBS2026-05-10T00:00.zip",
        "OBS2026-05-12T00:00.zip",
        "OBS2026-05-11T00:00.zip",
    ]


def test_identity_from_config_pulls_station_and_uploader_fields(tmp_path: Path):
    cfg = _config(tmp_path)
    ident = identity_from_config(cfg)
    assert ident.call == "AC0G"
    assert ident.grid == "EM38ww"
    assert ident.station_id == "S000082"
    assert ident.ssh_key_file == "/etc/hs-uploader/keys/id_ed25519"


def test_transport_from_config_dry_run_flag_flows_through(tmp_path: Path):
    cfg = _config(tmp_path)
    t = transport_from_config(cfg, dry_run=True)
    assert t.dry_run is True
    assert t.instrument_id == "RM3100"
    assert t.host == "pswsnetwork.eng.ua.edu"


def test_transport_from_config_bandwidth_zero_translates_to_unlimited(tmp_path: Path):
    """0 is the operator-facing 'no cap' sentinel.  build_transport
    must translate it to None so the transport omits the sftp -l
    flag -- passing -l 0 would stall the upload, not unlimit it.
    Anything non-zero passes through verbatim."""
    cfg = _config(tmp_path)
    cfg["uploader"]["bandwidth_limit_kbps"] = 0
    assert transport_from_config(cfg).bandwidth_limit_kbps is None

    cfg["uploader"]["bandwidth_limit_kbps"] = 100
    assert transport_from_config(cfg).bandwidth_limit_kbps == 100

    cfg["uploader"]["bandwidth_limit_kbps"] = None
    assert transport_from_config(cfg).bandwidth_limit_kbps is None


# ---- drain_queue() -----------------------------------------------------------


def test_drain_queue_dry_run_does_not_delete_zips(tmp_path: Path):
    """In dry-run mode the transport acks each upload but local zips stay."""
    cfg = _config(tmp_path)
    queue = _make_queue(tmp_path, ["2026-05-10", "2026-05-11"])

    with patch("subprocess.run") as run_mock:
        acked, failed, remaining = drain_queue(queue, cfg, dry_run=True)

    assert acked == 2
    assert failed == 0
    assert remaining == []
    # Dry-run never invokes sftp.
    run_mock.assert_not_called()
    # And it preserves the local zips so a later real run can ship them.
    assert sorted(p.name for p in queue.glob("OBS*.zip")) == [
        "OBS2026-05-10T00:00.zip",
        "OBS2026-05-11T00:00.zip",
    ]


def test_drain_queue_real_run_deletes_acked_zips(tmp_path: Path):
    cfg = _config(tmp_path)
    queue = _make_queue(tmp_path, ["2026-05-10", "2026-05-11"])

    def _ok(*args, **kwargs):
        from unittest.mock import MagicMock
        res = MagicMock(); res.returncode = 0
        res.stdout = b""; res.stderr = b""
        return res

    with patch("subprocess.run", side_effect=_ok):
        acked, failed, remaining = drain_queue(queue, cfg, dry_run=False)

    assert acked == 2
    assert failed == 0
    assert list(queue.glob("OBS*.zip")) == []


def test_drain_queue_stops_on_first_failure(tmp_path: Path):
    """retry_later from PSWS should leave the failing zip + all later ones."""
    cfg = _config(tmp_path)
    queue = _make_queue(tmp_path, ["2026-05-10", "2026-05-11", "2026-05-12"])

    call_count = {"n": 0}
    def _first_ok_then_fail(*args, **kwargs):
        from unittest.mock import MagicMock
        call_count["n"] += 1
        res = MagicMock()
        res.returncode = 0 if call_count["n"] == 1 else 2
        res.stdout = b""
        res.stderr = b"" if call_count["n"] == 1 else b"sftp: Connection reset\n"
        return res

    with patch("subprocess.run", side_effect=_first_ok_then_fail):
        acked, failed, remaining = drain_queue(queue, cfg, dry_run=False)

    assert acked == 1
    assert failed == 1
    # First zip got shipped & deleted; the failing one and the one
    # behind it should both still be on disk.
    assert sorted(p.name for p in queue.glob("OBS*.zip")) == [
        "OBS2026-05-11T00:00.zip",
        "OBS2026-05-12T00:00.zip",
    ]
    assert [p.name for p in remaining] == [
        "OBS2026-05-11T00:00.zip",
        "OBS2026-05-12T00:00.zip",
    ]


def test_drain_queue_max_uploads_caps(tmp_path: Path):
    cfg = _config(tmp_path)
    queue = _make_queue(tmp_path, ["2026-05-10", "2026-05-11", "2026-05-12"])

    with patch("subprocess.run"):
        acked, failed, remaining = drain_queue(
            queue, cfg, dry_run=True, max_uploads=2,
        )
    assert acked == 2
    assert failed == 0
    assert [p.name for p in remaining] == ["OBS2026-05-12T00:00.zip"]


def test_drain_queue_empty_returns_zero(tmp_path: Path):
    cfg = _config(tmp_path)
    queue = tmp_path / "queue"; queue.mkdir()
    acked, failed, remaining = drain_queue(queue, cfg, dry_run=True)
    assert (acked, failed, remaining) == (0, 0, [])
