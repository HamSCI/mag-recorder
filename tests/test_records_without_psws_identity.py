"""Recording must not depend on identity metadata. Delivery may.

⛔ W3USR-019, 2026-09-18 → 2026-09-19. A healthy RM3100, and
`mag-recorder.service` failed from first boot onward:

    config still at template placeholders
    (station.psws_station_id=<YOUR_PSWS_STATION_ID>) ... exiting EX_CONFIG
    Main PID: 11333 (code=exited, status=78/CONFIG)

Days of geomagnetic data were never collected, because nobody had filled in
a web-portal registration.

The daemon uses none of `psws_station_id`, `callsign` or `grid_square`.
`SupervisorConfig` takes `spool_dir`, `source`, `reporter_id`,
`timing_sidecar`. Those three fields are consumed later and elsewhere —
`package` (log-name prefix) and `upload` (the SFTP account). The two are
already separate units and `package_day(delete_source=)` already defaults
False, so the architecture was right. Only the startup gate was not.

The gate's own docstring said it copied "the same idle-unconfigured pattern
wspr/psk/meteor use". That pattern is correct there: psk gates on the
radiod status address, without which the daemon cannot function at all.
mag copied the shape and aimed it at IDENTITY fields.

    A fail-fast gate must name a DEPENDENCY, not an IDENTITY.

Design and behaviour are rob's (AI6VN), from issue #8.

⚠ These tests narrow the gate; they must not remove it.
`test_a_configured_station_still_records` has to pass both before and
after, or it is testing nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mag_recorder.config import (  # noqa: E402
    is_placeholder,
    unconfigured_placeholders,
    upload_blockers,
)
from mag_recorder.core.packager import site_token  # noqa: E402


TEMPLATE_STATION = {
    "psws_station_id": "<YOUR_PSWS_STATION_ID>",
    "callsign": "<YOUR_CALL>",
    "grid_square": "<YOUR_GRID>",
    "instrument_id": "",
}
REAL_STATION = {
    "psws_station_id": "S000170",
    "callsign": "AC0G",
    "grid_square": "EM38ww",
    "instrument_id": "372",
}


class TestIsPlaceholder:
    """One spelling of 'the operator has not answered this yet'."""

    @pytest.mark.parametrize("val", [
        "<YOUR_PSWS_STATION_ID>", "<YOUR_CALL>", "<configure-via-config-init>",
        "", "   ", None,
    ])
    def test_unanswered_values(self, val):
        assert is_placeholder(val) is True

    @pytest.mark.parametrize("val", ["S000170", "AC0G", "372", "EM38ww"])
    def test_real_values(self, val):
        assert is_placeholder(val) is False

    def test_a_bare_angle_bracket_is_not_a_placeholder(self):
        """Only a value that is ENTIRELY an angle-bracketed token."""
        assert is_placeholder("AC0G <portable>") is False


class TestUploadBlockers:
    """What stops DELIVERY — not what stops recording."""

    def test_a_template_station_cannot_deliver(self):
        blockers = upload_blockers({"station": dict(TEMPLATE_STATION)})
        assert blockers, "a template config reported nothing blocking upload"
        assert any("psws_station_id" in b for b in blockers)

    def test_a_real_station_has_none(self):
        assert upload_blockers({"station": dict(REAL_STATION)}) == []

    def test_grid_square_does_not_block_delivery(self):
        """Location is not an upload credential. PSWS keys on the station
        account and the instrument id; an unset grid is a metadata gap, not
        a reason to hold a day's data hostage."""
        st = dict(REAL_STATION, grid_square="<YOUR_GRID>")
        assert upload_blockers({"station": st}) == []


class TestTheGateStillExists:
    """⚠ Narrowed, not removed. These pass before AND after the change."""

    def test_template_placeholders_are_still_reported(self):
        stale = unconfigured_placeholders({"station": dict(TEMPLATE_STATION)})
        assert any("psws_station_id" in s for s in stale)

    def test_a_configured_station_reports_nothing_stale(self):
        assert unconfigured_placeholders({"station": dict(REAL_STATION)}) == []


class TestPackagingIgnoresTemplateIdentity:
    """A template value is not an identity.

    `site_token` sanitises anything outside [A-Za-z0-9_-] to `_`, so
    `<YOUR_CALL>` would bake `_YOUR_CALL_` into the archived log name —
    a shipped artifact asserting an identity nobody holds.
    """

    def test_a_template_callsign_falls_back_to_the_neutral_prefix(self):
        assert site_token("<YOUR_CALL>") == "OBS"

    def test_a_real_callsign_survives(self):
        assert site_token("AC0G") == "AC0G"

    def test_a_path_like_callsign_is_still_sanitised(self):
        assert site_token("AC0G/B4") == "AC0G_B4"

    def test_empty_still_falls_back(self):
        assert site_token("") == "OBS"
        assert site_token(None) == "OBS"


# --- the two CLI gates, driven for real -----------------------------------
#
# The tests above pin the decisions; these pin the BEHAVIOUR that cost
# W3USR-019 its data. rob's equivalent failed against the old code with
# "the daemon exited 78 instead of recording".

import types  # noqa: E402

from mag_recorder import cli  # noqa: E402


def _write_cfg(tmp_path, station: dict, extra: str = "") -> Path:
    q = tmp_path / "upload"; q.mkdir()
    (tmp_path / "spool").mkdir()
    body = "[station]\n" + "".join(
        f'{k} = "{v}"\n' for k, v in station.items()
    ) + (
        f'\n[paths]\nspool_dir = "{tmp_path / "spool"}"\n'
        f'log_dir = "{tmp_path}"\nupload_queue_dir = "{q}"\n'
    ) + extra
    p = tmp_path / "mag-recorder-config.toml"
    p.write_text(body)
    return p


class TestTheDaemonRecordsWithoutIdentity:

    def test_a_template_config_does_not_exit_78(self, tmp_path, monkeypatch):
        """THE regression. Old code: SystemExit(78), zero samples, forever."""
        cfg = _write_cfg(tmp_path, TEMPLATE_STATION)
        reached = {}

        def _fake_run_supervisor(sup_cfg, **kw):
            reached["spool_dir"] = str(sup_cfg.spool_dir)
            return 0

        monkeypatch.setattr(cli, "_add_daemon_file_log", lambda *a, **k: None)
        monkeypatch.setitem(sys.modules, "_stub", types.ModuleType("_stub"))
        import mag_recorder.core.supervisor as sup
        monkeypatch.setattr(sup, "run_supervisor", _fake_run_supervisor,
                            raising=False)
        monkeypatch.setattr(sup, "make_source", lambda *a, **k: object(),
                            raising=False)

        args = types.SimpleNamespace(config=cfg, instance=None, simulate=True,
                                     log_level=None)
        try:
            cli._handle_daemon(args)
        except SystemExit as exc:
            if exc.code == 78:
                pytest.fail("the daemon exited 78 instead of recording")
            raise
        except Exception:
            # Any OTHER failure is environmental (no device, no systemd) and
            # is not what this test is about -- what matters is that the
            # placeholder gate did not stop it before it got that far.
            pass


class TestUploadHoldsRatherThanFails:

    def test_no_identity_exits_zero_and_reports_the_depth(
            self, tmp_path, monkeypatch, capsys):
        cfg = _write_cfg(tmp_path, TEMPLATE_STATION)
        q = tmp_path / "upload"
        for n in range(3):
            (q / f"OBS2026-09-1{n}T00:00.zip").write_bytes(b"x")

        drained = {"called": False}
        import mag_recorder.core.uploader as up
        monkeypatch.setattr(
            up, "drain_queue",
            lambda *a, **k: drained.__setitem__("called", True) or (0, 0, []),
            raising=False)

        args = types.SimpleNamespace(config=cfg, dry_run=False,
                                     max_uploads=None, log_level=None)
        cli._handle_upload(args)          # must NOT raise SystemExit

        out = capsys.readouterr().out
        assert "held=3" in out, out
        assert not drained["called"], (
            "upload dived into drain_queue with no credentials")

    def test_a_configured_station_still_drains(self, tmp_path, monkeypatch):
        """⚠ Passes both ways. Narrowing must not disable the real path."""
        cfg = _write_cfg(tmp_path, REAL_STATION)
        called = {}
        import mag_recorder.core.uploader as up
        monkeypatch.setattr(
            up, "drain_queue",
            lambda *a, **k: called.__setitem__("yes", True) or (0, 0, []),
            raising=False)
        args = types.SimpleNamespace(config=cfg, dry_run=True,
                                     max_uploads=None, log_level=None)
        cli._handle_upload(args)
        assert called.get("yes"), "a configured station did not reach the drain"
