"""mag-recorder probes its I2C addresses at install (v3.83 row 3)."""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from mag_recorder.i2c_probe import choose, parse_scan, probe, write_addresses

TEMPLATE = (Path(__file__).resolve().parents[1]
            / "config" / "mag-recorder-config.toml.template")
TRANSCRIPT = (Path(__file__).resolve().parents[2]
              / "mag-usb" / "assets" / "mag-usb_output.txt")


def _cfg(tmp_path: Path) -> Path:
    p = tmp_path / "mag-recorder-config.toml"
    shutil.copy(TEMPLATE, p)
    return p


def _load(p: Path) -> dict:
    with open(p, "rb") as f:
        return tomllib.load(f)


def test_parse_scan_reads_the_real_transcript() -> None:
    if not TRANSCRIPT.exists():
        pytest.skip("sibling mag-usb checkout absent")
    # The captured scan failed, so it lists no address.
    assert parse_scan(TRANSCRIPT.read_text()) == []
    sample = "    Found 2 device(s):\n      Address: 0x19\n      Address: 0x20\n"
    assert parse_scan(sample) == [0x19, 0x20]


def test_choose_picks_one_per_range() -> None:
    assert choose([0x19, 0x20]) == (0x20, 0x19, [])


def test_choose_refuses_two_in_a_range() -> None:
    mag, temp, warns = choose([0x20, 0x23])
    assert mag is None
    assert any("0x20" in w and "0x23" in w for w in warns)


def test_choose_with_nothing_found() -> None:
    mag, temp, warns = choose([])
    assert (mag, temp) == (None, None)
    assert len(warns) == 2


def test_write_replaces_the_mag_address_and_adds_a_live_temp_line(
        tmp_path: Path) -> None:
    p = _cfg(tmp_path)
    write_addresses(p, 0x20, 0x19)
    mag = _load(p)["mag"]
    assert mag["i2c_address"] == 0x20
    assert mag["remote_temp_address"] == 0x19
    text = p.read_text()
    assert "# remote_temp_address = 0x1F" in text
    assert "i2c_address      = 0x20" in text or "i2c_address = 0x20" in text
    assert "0x19" in text


def test_write_with_none_leaves_the_key_alone(tmp_path: Path) -> None:
    p = _cfg(tmp_path)
    write_addresses(p, None, 0x19)
    mag = _load(p)["mag"]
    assert mag["i2c_address"] == 0x23
    assert mag["remote_temp_address"] == 0x19
    (tmp_path / "q").mkdir()
    q = _cfg(tmp_path / "q")
    before = q.read_bytes()
    write_addresses(q, None, None)
    assert q.read_bytes() == before


def _no_run(*a, **k):
    raise AssertionError("runner must not be called")


def test_probe_without_the_device_exits_zero_and_writes_nothing(
        tmp_path: Path) -> None:
    p = _cfg(tmp_path)
    before = p.read_bytes()
    assert probe(p, device=tmp_path / "nope", runner=_no_run) == 0
    assert p.read_bytes() == before


def test_probe_survives_a_hung_scan(tmp_path: Path, capsys) -> None:
    p = _cfg(tmp_path)
    dev = tmp_path / "ttyMAG0"
    dev.touch()
    before = p.read_bytes()

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="mag-usb", timeout=10)

    assert probe(p, device=dev, runner=hang) == 0
    assert p.read_bytes() == before
    assert "timed out" in capsys.readouterr().out


def test_probe_survives_a_failed_scan(tmp_path: Path) -> None:
    p = _cfg(tmp_path)
    dev = tmp_path / "ttyMAG0"
    dev.touch()
    before = p.read_bytes()

    def fail(*a, **k):
        return SimpleNamespace(returncode=1, stdout="",
                               stderr="No I2C devices found.\n")

    assert probe(p, device=dev, runner=fail) == 0
    assert p.read_bytes() == before


def test_probe_writes_what_answers(tmp_path: Path) -> None:
    p = _cfg(tmp_path)
    dev = tmp_path / "ttyMAG0"
    dev.touch()

    def ok(*a, **k):
        return SimpleNamespace(
            returncode=0, stdout="    Found 2 device(s):\n",
            stderr="      Address: 0x19\n      Address: 0x20\n")

    assert probe(p, device=dev, runner=ok) == 0
    mag = _load(p)["mag"]
    assert (mag["i2c_address"], mag["remote_temp_address"]) == (0x20, 0x19)
