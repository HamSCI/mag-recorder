"""sigmond#54 — one owner for the magnetometer upload queue.

Two uploaders drained /var/lib/mag-recorder/upload on B4: this repo's
mag-recorder-upload.timer (package + its own upload, delete-on-ack) and the
hs-uploader daemon's mag-psws pipeline (retention=keep, so it never deleted
and the timer re-shipped what the daemon had already sent).  Contract now:
the hs-uploader daemon ships and deletes; the timer only packages while a
daemon is active, and ships itself only when running standalone."""
from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _unit(name: str) -> str:
    return (REPO / "systemd" / name).read_text()


def test_upload_timer_ships_only_when_no_daemon_is_active():
    txt = _unit("mag-recorder-upload.service")
    exec_lines = [l for l in txt.splitlines() if l.startswith("ExecStart=")]
    assert any("cli package" in l for l in exec_lines)
    upload = [l for l in exec_lines if "cli upload" in l]
    assert len(upload) == 1
    assert "systemctl is-active --quiet hs-uploader" in upload[0], upload[0]


def test_upload_timer_writes_group_writable_zips():
    txt = _unit("mag-recorder-upload.service")
    assert "UMask=0002" in txt


def test_recorder_unit_makes_the_queue_group_writable_for_hsupload():
    txt = _unit("mag-recorder.service")
    assert "hsupload" in txt and "2775" in txt and "/var/lib/mag-recorder/upload" in txt
    # the setgid group step must come AFTER the chown -R that would otherwise undo it
    lines = txt.splitlines()
    chown = next(i for i, l in enumerate(lines) if "chown -R magrec:magrec /var/lib/mag-recorder" in l)
    chgrp = next(i for i, l in enumerate(lines) if "hsupload" in l and "2775" in l)
    assert chgrp > chown


def test_daemon_pipeline_deletes_on_ack():
    d = tomllib.loads((REPO / "deploy.toml").read_text())
    pipes = d["hs_uploader"]["pipeline"]
    mag = next(p for p in pipes if p["name"] == "mag-psws")
    assert mag["source"]["retention"] == "delete_on_ack"
