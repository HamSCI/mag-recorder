"""An unset instrument id must stay unset — never the sensor model.

PSWS ISSUES an instrument id per instrument: a short number, `372` on
AC0G-B4. It rides in the upload trigger

    m<dataset>_#<instrument_id>_#<upload-time>

so a wrong value uploads *successfully*, the zip lands, and PSWS cannot
match it to an instrument. Nothing fails on our side, which is why this
went unnoticed: the failure is silent and remote.

mag-recorder used to default the field to `"RM3100"` — the sensor model.
That is worse than an empty value in three separate ways:

1. It ships a well-formed WRONG trigger rather than refusing to ship.
2. sigmond's `upload_creds._is_placeholder()` counts only empty or
   `"<YOUR_…>"` as unset, so the model name read as CONFIGURED and
   suppressed the credential prompt on a station that had configured
   nothing.
3. It is plausible. A reviewer sees the right sensor in the right field
   and moves on.

The whole existing suite passed with the default in place, because every
fixture sets the field explicitly. These tests exercise the ABSENCE.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mag_recorder.config import DEFAULTS
from mag_recorder.core.uploader import transport_from_config


def test_the_config_default_is_empty():
    assert DEFAULTS["station"]["instrument_id"] == ""


def test_a_config_with_no_station_block_yields_no_instrument_id():
    """The transport must not invent one when the operator supplied none."""
    t = transport_from_config({}, dry_run=True)
    assert t.instrument_id == "", (
        f"transport invented instrument_id={t.instrument_id!r} from an "
        "empty config")


def test_the_sensor_model_is_never_substituted():
    """Pin the specific wrong value, so a helpful re-add fails loudly."""
    t = transport_from_config({"station": {}}, dry_run=True)
    assert t.instrument_id != "RM3100"


def test_an_operator_supplied_id_still_reaches_the_transport():
    """Narrowing the default must not break the configured path."""
    t = transport_from_config({"station": {"instrument_id": "372"}},
                              dry_run=True)
    assert t.instrument_id == "372"
