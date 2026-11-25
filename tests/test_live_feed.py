"""The mag live feed offers each sample to live-publisher's socket, best effort."""

from __future__ import annotations

import socket
import struct

from mag_recorder.core.live_feed import HEAD, LiveFeed, sample_frame
from mag_recorder.core.supervisor import SupervisorConfig, run_supervisor


def _s(**kw) -> dict:
    d = {"ts": "2026-11-25T18:00:01.250Z", "rt": 21.5, "x": 1.5, "y": -2.25, "z": 40000.0}
    d.update(kw)
    return d


def test_frame_carries_time_and_four_values() -> None:
    f = sample_frame(_s())
    magic, ver, kind, t, f0, df, db0, step, nlen, nbins = HEAD.unpack_from(f)
    assert (magic, ver, kind, nlen, nbins) == (b"LS", 1, 2, 3, 16)
    assert t == 1795629601.25
    assert f[HEAD.size:HEAD.size + 3] == b"mag"
    assert struct.unpack("<4f", f[HEAD.size + 3:]) == (1.5, -2.25, 40000.0, 21.5)
    assert len(f) == HEAD.size + 3 + 16


def test_bad_samples_make_no_frame() -> None:
    assert sample_frame(_s(x=float("nan"))) is None
    assert sample_frame(_s(ts="garbage")) is None
    s = _s()
    del s["rt"]
    assert sample_frame(s) is None


def test_no_socket_drops_quietly() -> None:
    feed = LiveFeed("/nonexistent/feeds.sock")
    feed.offer(_s())
    assert (feed.sent, feed.dropped) == (0, 1)


def test_supervisor_offers_each_spooled_sample(tmp_path) -> None:
    path = str(tmp_path / "feeds.sock")
    rx = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    rx.bind(path)
    rx.setblocking(False)
    cfg = SupervisorConfig(spool_dir=tmp_path / "spool",
                           source=iter([_s(), _s(x=0, y=0, z=0), _s(z=1.0)]),
                           live_feed=LiveFeed(path))
    run_supervisor(cfg)
    got = []
    while True:
        try:
            got.append(rx.recv(256))
        except BlockingIOError:
            break
    # the all-zero sample never spools, so it never goes live either
    assert len(got) == 2
    assert struct.unpack("<4f", got[1][HEAD.size + 3:])[2] == 1.0
