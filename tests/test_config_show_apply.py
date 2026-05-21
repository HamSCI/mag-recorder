"""Cover the JSON I/O the whiptail wizard depends on.

`config show --json --defaults` must emit valid JSON that contains
every section the wizard might prompt for.  `config apply --json -`
must round-trip those values back atomically and reject malformed
input rather than corrupt the file.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tomllib
from pathlib import Path

import pytest

from mag_recorder import configurator
from mag_recorder.config import DEFAULTS


def _ns(**kw) -> argparse.Namespace:
    """Shim for argparse Namespace; defaults all flags False/None."""
    base = {"config": None, "defaults": False, "json": True,
            "non_interactive": False, "reconfig": False, "log_level": None,
            "path": "-"}
    base.update(kw)
    return argparse.Namespace(**base)


# ---------- config show -----------------------------------------------------

def test_show_defaults_emits_every_section(tmp_path: Path, capsys) -> None:
    """`--defaults` must surface every section the wizard might prompt for,
    even if the operator's TOML is empty / missing -- the wizard relies on
    this to seed initial values."""
    rv = configurator.cmd_config_show(_ns(config=tmp_path / "nope.toml",
                                         defaults=True))
    assert rv == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert set(DEFAULTS.keys()).issubset(set(payload.keys()))
    # Must include the keys the wizard primary screen asks about.
    assert "psws_station_id" in payload["station"]
    assert "callsign"        in payload["station"]
    assert "i2c_address"     in payload["mag"]


def test_show_without_defaults_returns_file_contents(tmp_path: Path, capsys) -> None:
    """Without --defaults, the output mirrors the on-disk TOML so tooling
    can tell what the operator actually overrode vs what's a default."""
    config = tmp_path / "c.toml"
    config.write_text('[station]\ncallsign = "AC0G"\n')
    rv = configurator.cmd_config_show(_ns(config=config, defaults=False))
    assert rv == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"station": {"callsign": "AC0G"}}


def test_show_missing_file_without_defaults_returns_empty(tmp_path: Path, capsys) -> None:
    rv = configurator.cmd_config_show(_ns(config=tmp_path / "nope.toml",
                                         defaults=False))
    assert rv == 0
    assert json.loads(capsys.readouterr().out) == {}


# ---------- config apply ----------------------------------------------------

def _apply(payload: dict, tmp_path: Path, *, existing: str = "") -> int:
    """Drive cmd_config_apply with `payload` as stdin and return its exit code."""
    config = tmp_path / "c.toml"
    if existing:
        config.write_text(existing)
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    try:
        return configurator.cmd_config_apply(_ns(config=config))
    finally:
        sys.stdin = old_stdin


def test_apply_roundtrips(tmp_path: Path) -> None:
    """A valid payload writes back to the TOML and re-parses unchanged."""
    config = tmp_path / "c.toml"
    rv = _apply({"station": {"callsign": "AC0G", "grid_square": "EM38ww"},
                 "mag":     {"i2c_address": 0x23, "cycle_count": 400}},
                tmp_path)
    assert rv == 0
    with open(config, "rb") as f:
        loaded = tomllib.load(f)
    assert loaded["station"]["callsign"]   == "AC0G"
    assert loaded["station"]["grid_square"] == "EM38ww"
    assert loaded["mag"]["i2c_address"]    == 0x23
    assert loaded["mag"]["cycle_count"]    == 400


def test_apply_deep_merges_with_existing(tmp_path: Path) -> None:
    """Partial payloads (the common wizard case) must preserve everything
    the operator had set previously."""
    existing = '[station]\ncallsign = "OLD"\ngrid_square = "EN50aa"\n'
    rv = _apply({"station": {"callsign": "NEW"}}, tmp_path, existing=existing)
    assert rv == 0
    with open(tmp_path / "c.toml", "rb") as f:
        loaded = tomllib.load(f)
    assert loaded["station"]["callsign"]    == "NEW"   # overwritten
    assert loaded["station"]["grid_square"] == "EN50aa"  # preserved


def test_apply_rejects_unknown_section(tmp_path: Path, capsys) -> None:
    rv = _apply({"bogus": {"x": 1}}, tmp_path)
    assert rv == 2
    assert "unknown section" in capsys.readouterr().err.lower()


def test_apply_rejects_wrong_type(tmp_path: Path, capsys) -> None:
    """i2c_address is an int in DEFAULTS; a string must be rejected."""
    rv = _apply({"mag": {"i2c_address": "not-a-number"}}, tmp_path)
    assert rv == 2
    assert "expects number" in capsys.readouterr().err.lower()


def test_apply_rejects_cycle_count_out_of_range(tmp_path: Path, capsys) -> None:
    """driver_config.render() guards: cc must be 1..800."""
    rv = _apply({"mag": {"cycle_count": 1000}}, tmp_path)
    assert rv == 2
    err = capsys.readouterr().err.lower()
    assert "rejected by driver_config" in err or "cycle_count" in err


def test_apply_rejects_address_out_of_range(tmp_path: Path, capsys) -> None:
    rv = _apply({"mag": {"i2c_address": 0x80}}, tmp_path)
    assert rv == 2


def test_apply_rejects_invalid_sampling_mode(tmp_path: Path, capsys) -> None:
    rv = _apply({"mag": {"sampling_mode": "FAST"}}, tmp_path)
    assert rv == 2


def test_apply_is_atomic_part_rename(tmp_path: Path) -> None:
    """The write must go via .part + rename so a crash mid-write leaves
    the old file intact.  Verify the .part file is gone afterwards."""
    rv = _apply({"station": {"callsign": "AC0G"}}, tmp_path,
                existing='[station]\ncallsign = "OLD"\n')
    assert rv == 0
    assert (tmp_path / "c.toml").exists()
    assert not (tmp_path / "c.toml.part").exists()


def test_apply_rejects_non_object_payload(tmp_path: Path) -> None:
    """JSON must be an object at the top level, not e.g. a list."""
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(["not", "a", "dict"]))
    try:
        rv = configurator.cmd_config_apply(_ns(config=tmp_path / "c.toml"))
    finally:
        sys.stdin = old_stdin
    assert rv == 2


def test_apply_rejects_invalid_json(tmp_path: Path) -> None:
    old_stdin = sys.stdin
    sys.stdin = io.StringIO("this is not json {{ broken")
    try:
        rv = configurator.cmd_config_apply(_ns(config=tmp_path / "c.toml"))
    finally:
        sys.stdin = old_stdin
    assert rv == 2


# ---------- _serialize_toml (used by apply, worth pinning) ------------------

def test_serialize_toml_round_trips_via_tomllib(tmp_path: Path) -> None:
    """Anything we serialize must parse back through stdlib tomllib."""
    src = {
        "station":  {"callsign": "AC0G", "psws_station_id": "S000082"},
        "uploader": {"enabled": True, "bandwidth_limit_kbps": 100},
        "mag":      {"i2c_address": 0x23, "orientation": {"x": 0, "y": 90, "z": -90}},
        "simulator": {"baseline_x_nt": 21500.0},
    }
    text = configurator._serialize_toml(src)
    loaded = tomllib.loads(text)
    assert loaded == src


def test_serialize_toml_escapes_quotes_in_strings() -> None:
    text = configurator._serialize_toml({"station": {"description": 'has "quotes" and \\ slash'}})
    loaded = tomllib.loads(text)
    assert loaded["station"]["description"] == 'has "quotes" and \\ slash'


def test_serialize_toml_handles_nested_orientation() -> None:
    """[mag.orientation] should round-trip as a nested table."""
    text = configurator._serialize_toml({
        "mag": {"i2c_address": 0x23, "orientation": {"x": 90, "y": 0, "z": 0}},
    })
    assert "[mag]" in text
    assert "[mag.orientation]" in text
    loaded = tomllib.loads(text)
    assert loaded["mag"]["orientation"]["x"] == 90


# ---------- _wizard_available -----------------------------------------------

def test_wizard_available_false_without_tty(monkeypatch) -> None:
    """When stdin/stdout isn't a TTY, the wizard must not be invoked.
    Otherwise piping `mag-recorder config init` would hang on whiptail."""
    # In pytest stdout is not a TTY; verify the guard catches this.
    assert configurator._wizard_available() is False


def test_wizard_available_false_when_script_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(configurator, "_WIZARD_PATH", tmp_path / "nope.sh")
    assert configurator._wizard_available() is False
