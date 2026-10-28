"""The recorder refuses all-zero magnetometer samples (v3.83 row 3).

A wrong I2C address NACKs and mag-usb reports x=y=z=0.  Those are not
data; the guard drops them and logs on transitions only.
"""

from __future__ import annotations

import logging

from mag_recorder.core.supervisor import (
    SupervisorConfig,
    ZeroGuard,
    run_supervisor,
)


def _s(x=0, y=0, z=0, rt=21.5) -> dict:
    return {"ts": "x", "rt": rt, "x": x, "y": y, "z": z}


def test_zero_sample_is_refused() -> None:
    g = ZeroGuard()
    assert g.admit(_s()) is False
    assert g.dropped == 1


def test_nonzero_sample_passes() -> None:
    g = ZeroGuard()
    assert g.admit(_s(z=1)) is True
    assert g.admit(_s(x=0, y=0, z=0, rt=0)) is False  # rt plays no part
    assert g.admit(_s(x=3, y=0, z=0, rt=0)) is True
    assert g.dropped == 1


def test_logs_once_per_transition(caplog) -> None:
    g = ZeroGuard()
    with caplog.at_level(logging.INFO):
        for _ in range(100):
            g.admit(_s())
        g.admit(_s(x=5))
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(warns) == 1
    assert len(infos) == 1
    assert "100" in infos[0].getMessage()


def test_supervisor_never_spools_a_zero_sample(tmp_path) -> None:
    cfg = SupervisorConfig(
        spool_dir=tmp_path,
        source=iter([_s(), _s(x=1, y=2, z=3)]),
    )
    run_supervisor(cfg)
    lines = [
        ln for f in tmp_path.glob("samples-*.jsonl")
        for ln in f.read_text().splitlines()
    ]
    assert len(lines) == 1
    assert '"x":1' in lines[0]


def test_zero_run_still_pings_watchdog(tmp_path) -> None:
    pings = []
    cfg = SupervisorConfig(
        spool_dir=tmp_path, source=iter([_s(), _s()]),
        watchdog_ping=lambda: pings.append(1),
    )
    run_supervisor(cfg)
    assert len(pings) == 2
