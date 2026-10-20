"""mag-usb's stderr reaches the journal once per condition, not once per
failed read (B4, 10-20: an RM3100 that NACKs wrote 10,800 lines an hour)."""

from __future__ import annotations

import logging
import sys
import threading

from mag_recorder.core import supervisor
from mag_recorder.core.stderr_relay import StderrRelay

NACK = "  POLL write failed: Address NACK: target device did not respond"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def relay(caplog, **kw):
    caplog.set_level(logging.DEBUG, logger="test-relay")
    clock = Clock()
    return StderrRelay(logging.getLogger("test-relay"), clock=clock, **kw), clock


def texts(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "test-relay"]


def test_one_hour_of_one_failure_a_second_prints_six_lines(caplog) -> None:
    r, clock = relay(caplog)
    for _ in range(3600):
        r.line(NACK)
        r.tick()
        clock.t += 1
    out = texts(caplog)
    assert out[0] == f"mag-usb: {NACK}"
    assert len(out) == 1 + 5          # first line, a count at 10..50 min
    assert all("still repeating" in m for m in out[1:])
    assert sum(int(m.split()[1]) for m in out[1:]) == 600 * 5
    assert caplog.records[0].levelno == logging.WARNING


def test_a_line_that_stops_gets_a_clear_line_with_its_total(caplog) -> None:
    r, clock = relay(caplog)
    for _ in range(30):
        r.line(NACK)
        clock.t += 1
    clock.t += 58
    r.tick()
    assert len(texts(caplog)) == 1
    clock.t += 1
    r.tick()
    out = texts(caplog)
    assert out[-1] == f"mag-usb: cleared after 30 times over 29 s: {NACK}"
    # It returns: a new condition, printed again.
    r.line(NACK)
    assert texts(caplog)[-1] == f"mag-usb: {NACK}"


def test_a_line_seen_once_prints_once_and_no_clear_line(caplog) -> None:
    r, clock = relay(caplog)
    r.line("Using configuration file: '/run/mag-recorder/x.toml'.")
    clock.t += 3600
    r.tick()
    out = texts(caplog)
    assert len(out) == 1
    assert caplog.records[0].levelno == logging.INFO


def test_lines_that_differ_in_their_numbers_count_as_one(caplog) -> None:
    r, clock = relay(caplog)
    for n in range(100):
        r.line(f"  Timeout waiting for DRDY (status=0x{n:02X}) try {n}")
        clock.t += 1
    assert len(texts(caplog)) == 1


def test_too_many_distinct_lines_are_counted_not_printed(caplog) -> None:
    r, clock = relay(caplog, max_lines=4)
    for word in "abcdefghij":
        r.line(f"failure {word}")
    assert len(texts(caplog)) == 4
    clock.t += 600
    r.tick()
    assert "6 more lines" in texts(caplog)[-1]


def test_exit_prints_the_counts_not_yet_printed(caplog) -> None:
    r, clock = relay(caplog)
    for _ in range(5):
        r.line(NACK)
    r.flush()
    assert texts(caplog)[-1].startswith("mag-usb: 4 more times")


def test_the_supervisor_drains_stderr_through_the_relay(tmp_path, caplog) -> None:
    """A fake mag-usb writes 4,000 failure lines on stderr (more than a
    pipe holds) before its samples.  The source must yield the samples
    (no deadlock) and the journal must get a handful of lines."""
    fake = tmp_path / "mag-usb"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"for _ in range(4000): print({NACK!r}, file=sys.stderr)\n"
        "for n in range(3): print('{\"x\": %d}' % n, flush=True)\n")
    fake.chmod(0o755)
    caplog.set_level(logging.INFO, logger=supervisor.logger.name)
    got: list = []

    def run() -> None:
        got.extend(supervisor._mag_usb_source(
            str(fake), "/dev/null", 0x23, str(tmp_path / "d.toml")))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=30)
    assert not t.is_alive(), "the source deadlocked on an undrained stderr"
    assert got == [{"x": 0}, {"x": 1}, {"x": 2}]
    relayed = [r.getMessage() for r in caplog.records
               if r.getMessage().startswith("mag-usb: ")]
    assert relayed[0] == f"mag-usb: {NACK.strip()}"
    assert relayed[-1].startswith("mag-usb: 3999 more times in the last ")
    assert relayed[-1].endswith(f"still repeating: {NACK.strip()}")
    assert len(relayed) == 2
