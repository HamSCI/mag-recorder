"""Verify the mag-usb driver-TOML renderer.

The rendered file is what a real mag-usb -f reads, so the key names
(``cc_x``, ``nos_reg_value``, ``tmrc_rate``, ``portpath``, ``address``,
etc.) and section headers (``[i2c]``, ``[magnetometer]``,
``[mag_orientation]``, ``[temperature]``) must match upstream's
schema in mag-usb's docs/Configuration.md.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from mag_recorder.core.driver_config import render


def _load(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def test_render_writes_expected_schema(tmp_path: Path) -> None:
    dest = tmp_path / "mag-usb-driver.toml"
    render({
        "mag": {
            "device":      "/dev/ttyMAG0",
            "i2c_address": 0x23,
            "cycle_count": 400,
            "nos":         60,
            "tmrc_rate":   0x96,
        },
    }, dest)
    doc = _load(dest)
    assert doc["i2c"]["portpath"]            == "/dev/ttyMAG0"
    assert doc["i2c"]["use_I2C_converter"]   is True
    assert doc["magnetometer"]["address"]    == 0x23
    assert doc["magnetometer"]["cc_x"]       == 400
    assert doc["magnetometer"]["cc_y"]       == 400
    assert doc["magnetometer"]["cc_z"]       == 400
    assert doc["magnetometer"]["nos_reg_value"] == 60
    assert doc["magnetometer"]["tmrc_rate"]  == 0x96
    assert doc["magnetometer"]["sampling_mode"] == "POLL"
    assert doc["temperature"]["remote_temp_address"] == 0x1F
    assert doc["mag_orientation"]["mag_translate_x"] == 0


def test_render_picks_up_orientation(tmp_path: Path) -> None:
    dest = tmp_path / "drv.toml"
    render({
        "mag": {"orientation": {"x": 90, "y": -90, "z": 180}},
    }, dest)
    ori = _load(dest)["mag_orientation"]
    assert (ori["mag_translate_x"], ori["mag_translate_y"], ori["mag_translate_z"]) == (90, -90, 180)


def test_render_rejects_invalid_cycle_count(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cycle_count"):
        render({"mag": {"cycle_count": 1000}}, tmp_path / "drv.toml")
    with pytest.raises(ValueError, match="cycle_count"):
        render({"mag": {"cycle_count": 0}}, tmp_path / "drv.toml")


def test_render_rejects_invalid_address(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="i2c_address"):
        render({"mag": {"i2c_address": 0x80}}, tmp_path / "drv.toml")
    with pytest.raises(ValueError, match="i2c_address"):
        render({"mag": {"i2c_address": 0}}, tmp_path / "drv.toml")


def test_render_rejects_invalid_sampling_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sampling_mode"):
        render({"mag": {"sampling_mode": "FAST"}}, tmp_path / "drv.toml")


def test_render_is_atomic(tmp_path: Path) -> None:
    """Writes go to <dest>.part first, then rename to <dest> -- so a
    crash mid-write leaves the old file intact instead of half-written.
    Verify by inspecting that the .part file is gone after a successful
    render."""
    dest = tmp_path / "drv.toml"
    render({"mag": {"i2c_address": 0x23}}, dest)
    assert dest.exists()
    assert not (tmp_path / "drv.toml.part").exists()


def test_render_produces_auto_generated_warning(tmp_path: Path) -> None:
    dest = tmp_path / "drv.toml"
    render({"mag": {}}, dest)
    text = dest.read_text(encoding="utf-8")
    assert "AUTO-GENERATED" in text
    assert "DO NOT EDIT BY HAND" in text


def test_render_returns_dest_path(tmp_path: Path) -> None:
    dest = tmp_path / "drv.toml"
    out = render({"mag": {}}, dest)
    assert out == dest


def test_render_creates_parent_dirs(tmp_path: Path) -> None:
    dest = tmp_path / "nested" / "deeper" / "drv.toml"
    render({"mag": {}}, dest)
    assert dest.is_file()


def test_render_defaults_match_upstream_tools_config(tmp_path: Path) -> None:
    """No-overrides render should reproduce wittend/mag-usb's
    tools/config.toml values for the fields mag-recorder controls.
    Catches the regression where a default silently drifts away from
    upstream's recommended shipped values."""
    dest = tmp_path / "drv.toml"
    render({}, dest)  # entirely empty config -- all defaults
    doc = _load(dest)
    assert doc["magnetometer"]["cc_x"]            == 400
    assert doc["magnetometer"]["cc_y"]            == 400
    assert doc["magnetometer"]["cc_z"]            == 400
    assert doc["magnetometer"]["nos_reg_value"]   == 60
    assert doc["magnetometer"]["tmrc_rate"]       == 0x96
    assert doc["magnetometer"]["drdy_delay"]      == 10
    assert doc["magnetometer"]["sampling_mode"]   == "POLL"
    assert doc["temperature"]["remote_temp_address"] == 0x1F
