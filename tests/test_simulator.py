"""Simulator + supervisor smoke tests (no hardware required)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from mag_recorder.core.simulator import SimulatorConfig, generate_line, iter_samples
from mag_recorder.core.supervisor import (
    DailySpoolWriter,
    SupervisorConfig,
    _restamp,
    run_supervisor,
)


def test_generate_line_has_expected_fields():
    cfg = SimulatorConfig(seed=42)
    rng = __import__("random").Random(cfg.seed)
    line = generate_line(cfg, rng, t=datetime(2026, 5, 12, 23, 0, 0, tzinfo=timezone.utc))
    assert set(line.keys()) == {"ts", "rt", "x", "y", "z"}
    assert "2026" in line["ts"]
    # Sanity: realistic field magnitudes (nT), not zero.
    assert abs(line["x"] - cfg.baseline_x_nt) < 5 * cfg.noise_nt
    assert abs(line["z"] - cfg.baseline_z_nt) < 5 * cfg.noise_nt
    # Temperature near the baseline.
    assert abs(line["rt"] - cfg.baseline_rt_c) < 5 * cfg.noise_rt_c


def test_iter_samples_count_no_sleep():
    cfg = SimulatorConfig(seed=42)
    samples = list(iter_samples(cfg, count=5, sleep=False))
    assert len(samples) == 5
    for s in samples:
        assert "x" in s and "y" in s and "z" in s


def test_iter_samples_deterministic_with_seed():
    a = list(iter_samples(SimulatorConfig(seed=1), count=3, sleep=False))
    b = list(iter_samples(SimulatorConfig(seed=1), count=3, sleep=False))
    assert [s["x"] for s in a] == [s["x"] for s in b]


def test_restamp_produces_iso8601_ms():
    s = {"ts": "12 May 2026 23:45:01", "rt": 22.0, "x": 100.0, "y": 200.0, "z": 300.0}
    out = _restamp(s)
    # YYYY-MM-DDTHH:MM:SS.mmmZ
    assert len(out["ts"]) == len("2026-05-12T23:45:01.123Z")
    assert out["ts"][4] == "-" and out["ts"][7] == "-"
    assert out["ts"][10] == "T"
    assert out["ts"].endswith("Z")
    # Other fields preserved.
    assert out["x"] == 100.0
    assert out["rt"] == 22.0


def test_daily_spool_writer_writes_one_line_per_call(tmp_path: Path):
    writer = DailySpoolWriter(tmp_path)
    writer.write({"ts": "2026-05-12T00:00:00.000Z", "x": 1, "y": 2, "z": 3, "rt": 22.0})
    writer.write({"ts": "2026-05-12T00:00:01.000Z", "x": 4, "y": 5, "z": 6, "rt": 22.1})
    writer.close()

    target = tmp_path / "samples-2026-05-12.jsonl"
    assert target.is_file()
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["x"] == 1
    assert json.loads(lines[1])["z"] == 6


def test_daily_spool_writer_rotates_on_utc_date_change(tmp_path: Path):
    writer = DailySpoolWriter(tmp_path)
    writer.write({"ts": "2026-05-12T23:59:59.000Z", "x": 1, "y": 1, "z": 1, "rt": 22})
    writer.write({"ts": "2026-05-13T00:00:00.000Z", "x": 2, "y": 2, "z": 2, "rt": 22})
    writer.close()

    assert (tmp_path / "samples-2026-05-12.jsonl").is_file()
    assert (tmp_path / "samples-2026-05-13.jsonl").is_file()


def test_supervisor_pipes_simulator_into_spool(tmp_path: Path):
    """End-to-end: simulator -> supervisor -> daily JSONL on disk."""
    cfg = SimulatorConfig(seed=7)
    source = iter_samples(cfg, count=3, sleep=False)

    stop = threading.Event()
    pings = []
    sup = SupervisorConfig(
        spool_dir     = tmp_path,
        source        = source,
        watchdog_ping = lambda: pings.append(True),
    )
    run_supervisor(sup, stop_event=stop)

    # Three samples on disk, one file (today's date).
    files = list(tmp_path.glob("samples-*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    # Each line is well-formed JSON with the ISO-8601 ms timestamp.
    for ln in lines:
        d = json.loads(ln)
        assert "ts" in d and d["ts"].endswith("Z") and "." in d["ts"]
        assert "x" in d
    assert len(pings) == 3
