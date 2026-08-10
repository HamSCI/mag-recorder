"""TimingSidecar: chrony-sourced provenance lines beside the sample spool."""
import json
from pathlib import Path

from mag_recorder.core.timing_sidecar import TimingSidecar


def _block(source="chrony-sysclock", timing_source="CHRONY_FUSE",
           t_level="T4", stratum=1, leap="Normal", sigma=505_000):
    return {
        "source": source, "timing_source": timing_source,
        "t_level_active": t_level, "stratum": stratum, "leap_status": leap,
        "sigma_ns": sigma, "system_time_offset_ns": 8532,
    }


class _Clock:
    def __init__(self, t=1000.0): self.t = t
    def __call__(self): return self.t


def _lines(spool: Path, date="2026-08-10"):
    p = spool / f"timing-{date}.jsonl"
    if not p.is_file(): return []
    return [json.loads(l) for l in p.read_text().splitlines()]


def test_first_sample_writes(tmp_path):
    clk = _Clock()
    sc = TimingSidecar(tmp_path, block_fn=_block, now_fn=clk)
    sc.maybe_sample("2026-08-10T18:00:00.000Z")
    rows = _lines(tmp_path)
    assert len(rows) == 1
    assert rows[0]["ts"] == "2026-08-10T18:00:00.000Z"
    assert rows[0]["timing_source"] == "CHRONY_FUSE"


def test_no_rewrite_within_interval_or_heartbeat(tmp_path):
    clk = _Clock()
    sc = TimingSidecar(tmp_path, block_fn=_block, now_fn=clk,
                       interval_sec=60, heartbeat_sec=600)
    sc.maybe_sample("2026-08-10T18:00:00.000Z")
    clk.t += 61  # next poll happens, block unchanged -> no write
    sc.maybe_sample("2026-08-10T18:01:01.000Z")
    assert len(_lines(tmp_path)) == 1


def test_volatile_fields_do_not_trigger(tmp_path):
    clk = _Clock()
    calls = {"n": 0}
    def fn():
        calls["n"] += 1
        return _block(sigma=505_000 + calls["n"])  # sigma jitters every poll
    sc = TimingSidecar(tmp_path, block_fn=fn, now_fn=clk,
                       interval_sec=60, heartbeat_sec=600)
    sc.maybe_sample("2026-08-10T18:00:00.000Z")
    clk.t += 61
    sc.maybe_sample("2026-08-10T18:01:01.000Z")
    assert len(_lines(tmp_path)) == 1


def test_material_change_triggers(tmp_path):
    clk = _Clock()
    state = {"leap": "Normal"}
    sc = TimingSidecar(tmp_path, block_fn=lambda: _block(leap=state["leap"]),
                       now_fn=clk, interval_sec=60, heartbeat_sec=600)
    sc.maybe_sample("2026-08-10T18:00:00.000Z")
    state["leap"] = "Not synchronised"
    clk.t += 61
    sc.maybe_sample("2026-08-10T18:01:01.000Z")
    rows = _lines(tmp_path)
    assert len(rows) == 2 and rows[1]["leap_status"] == "Not synchronised"


def test_heartbeat_rewrites_unchanged_block(tmp_path):
    clk = _Clock()
    sc = TimingSidecar(tmp_path, block_fn=_block, now_fn=clk,
                       interval_sec=60, heartbeat_sec=600)
    sc.maybe_sample("2026-08-10T18:00:00.000Z")
    clk.t += 601
    sc.maybe_sample("2026-08-10T18:10:01.000Z")
    assert len(_lines(tmp_path)) == 2


def test_rotation_by_sample_date(tmp_path):
    clk = _Clock()
    sc = TimingSidecar(tmp_path, block_fn=_block, now_fn=clk,
                       interval_sec=60, heartbeat_sec=600)
    sc.maybe_sample("2026-08-10T23:59:59.000Z")
    clk.t += 601
    sc.maybe_sample("2026-08-11T00:00:59.000Z")
    assert len(_lines(tmp_path, "2026-08-10")) == 1
    assert len(_lines(tmp_path, "2026-08-11")) == 1


def test_block_fn_failure_never_raises(tmp_path):
    def boom(): raise RuntimeError("chrony exploded")
    sc = TimingSidecar(tmp_path, block_fn=boom, now_fn=_Clock())
    sc.maybe_sample("2026-08-10T18:00:00.000Z")  # must not raise
    assert _lines(tmp_path) == []
