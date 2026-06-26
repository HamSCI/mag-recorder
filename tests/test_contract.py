"""Contract v0.8 inventory + validate shape tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mag_recorder.config import DEFAULTS, load_config
from mag_recorder.contract import CONTRACT_VERSION, build_inventory, build_validate


def _write_config(tmp_path: Path, overrides: dict | None = None) -> Path:
    """Write a TOML at tmp_path that load_config() can read."""
    import tomli_w  # only needed in tests; not a runtime dep
    body: dict = {}
    for section, defaults in DEFAULTS.items():
        body[section] = dict(defaults)
    body["station"]["psws_station_id"] = "S000082"
    body["station"]["instrument_id"]   = "RM3100"
    body["station"]["callsign"]        = "AC0G"
    body["station"]["grid_square"]     = "EM38ww"
    body["simulator"]["enabled"]       = True  # avoid mag-usb-binary check
    if overrides:
        for section, fields in overrides.items():
            body.setdefault(section, {}).update(fields)
    path = tmp_path / "mag-recorder-config.toml"
    path.write_bytes(tomli_w.dumps(body).encode())
    return path


@pytest.fixture
def cfg(tmp_path):
    path = _write_config(tmp_path)
    return load_config(path), path


def test_inventory_shape(cfg):
    config, path = cfg
    inv = build_inventory(config, path)

    assert inv["client"] == "mag-recorder"
    assert inv["contract_version"] == CONTRACT_VERSION
    assert inv["config_path"] == str(path)
    assert "instances" in inv and len(inv["instances"]) == 1

    inst = inv["instances"][0]
    assert inst["instance"] == "default"

    # §16.5 — non-radiod client MUST omit these.
    assert "radiod_id"             not in inst
    assert "data_destination"      not in inst
    assert "chain_delay_ns_applied" not in inst

    # §16.3 — data_path required, kind=other for hardware that isn't radiod/kiwi/file.
    assert inst["data_path"]["kind"] == "other"
    assert "details" in inst["data_path"]
    assert "device" in inst["data_path"]["details"]

    # §17 — data_sinks per instance.
    assert isinstance(inst["data_sinks"], list)
    assert all(s["kind"] in ("file", "clickhouse") for s in inst["data_sinks"])
    # We are file-only for v0.1.
    assert all(s["kind"] == "file" for s in inst["data_sinks"])


def test_inventory_includes_psws_identity(cfg):
    config, path = cfg
    inv = build_inventory(config, path)
    inst = inv["instances"][0]
    assert inst["psws_station_id"] == "S000082"
    assert inst["instrument_id"] == "RM3100"


def test_validate_ok_with_simulator(cfg):
    config, path = cfg
    val = build_validate(config, path)
    # Simulator mode skips the mag-usb binary check; SSH key is the
    # only remaining warn-level issue when /etc/hs-uploader/keys is
    # absent in the test env -- a warn, not a fail.
    assert val["ok"] is True
    fail_issues = [i for i in val["issues"] if i["severity"] == "fail"]
    assert fail_issues == []


def test_validate_fail_without_station_id(tmp_path):
    path = _write_config(tmp_path, overrides={
        "station": {"psws_station_id": ""},
    })
    config = load_config(path)
    val = build_validate(config, path)
    assert val["ok"] is False
    msgs = [i["message"] for i in val["issues"]]
    assert any("psws_station_id" in m for m in msgs)


def test_validate_fail_without_mag_usb_binary_when_not_simulating(tmp_path):
    path = _write_config(tmp_path, overrides={
        "station":   {"psws_station_id": "S000082"},
        "simulator": {"enabled": False},
        "mag":       {"mag_usb_binary": "/nonexistent/bin/mag-usb"},
    })
    config = load_config(path)
    val = build_validate(config, path)
    assert val["ok"] is False
    msgs = [i["message"] for i in val["issues"]]
    assert any("mag-usb binary not found" in m for m in msgs)


def test_inventory_jsonable(cfg):
    """No exotic types in inventory -- must round-trip through json.dumps."""
    config, path = cfg
    s = json.dumps(build_inventory(config, path), indent=2)
    assert "mag-recorder" in s
    assert "RM3100" in s
