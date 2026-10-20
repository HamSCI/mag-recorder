"""Relay mag-usb's stderr to the journal without flooding it.

mag-usb prints its diagnostics on stderr, one set for each failed read.
A sensor that stops answering fails every second: B4's RM3100 ("Address
NACK") wrote three lines a second, 10,800 an hour, and a flood hides
every other line in the journal.

The relay prints each distinct line once.  While a line keeps repeating,
it prints one count every ``summary_s`` (10 min).  Once the line stops
for ``clear_s`` (1 min), it prints a clear line with the total.  Lines
that differ only in their numbers count as one line.
"""

from __future__ import annotations

import logging
import os
import re
import select
import time
from dataclasses import dataclass
from typing import Callable, Dict

_NUMBERS = re.compile(r"0x[0-9a-fA-F]+|\d+")
_TROUBLE = re.compile(r"error|fail|nack|timeout|unavailable|cannot|could not",
                      re.IGNORECASE)
_MAX_LINE = 64 * 1024


@dataclass
class _Repeat:
    text: str           # the first instance, as printed
    first_seen: float
    last_seen: float
    last_report: float
    pending: int = 0    # repeats since the last line this relay printed
    total: int = 1


class StderrRelay:
    def __init__(self, log: logging.Logger, *, summary_s: float = 600.0,
                 clear_s: float = 60.0, max_lines: int = 64,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._log = log
        self._summary_s = summary_s
        self._clear_s = clear_s
        self._max_lines = max_lines
        self._clock = clock
        self._seen: Dict[str, _Repeat] = {}
        self._overflow = 0
        self._overflow_since = 0.0

    def _level(self, text: str) -> int:
        return logging.WARNING if _TROUBLE.search(text) else logging.INFO

    def line(self, text: str) -> None:
        now = self._clock()
        key = _NUMBERS.sub("#", text)
        r = self._seen.get(key)
        if r is not None:
            r.pending += 1
            r.total += 1
            r.last_seen = now
            return
        if len(self._seen) >= self._max_lines:
            # Too many distinct lines at once: count them, print none.
            if not self._overflow:
                self._overflow_since = now
            self._overflow += 1
            return
        self._seen[key] = _Repeat(text, now, now, now)
        self._log.log(self._level(text), "mag-usb: %s", text)

    def tick(self) -> None:
        now = self._clock()
        for key, r in list(self._seen.items()):
            if now - r.last_seen >= self._clear_s:
                if r.total > 1:
                    self._log.info(
                        "mag-usb: cleared after %d times over %d s: %s",
                        r.total, int(r.last_seen - r.first_seen), r.text)
                del self._seen[key]
            elif r.pending and now - r.last_report >= self._summary_s:
                self._report(r, now)
        if self._overflow and now - self._overflow_since >= self._summary_s:
            self._log.warning(
                "mag-usb: %d more lines in the last %d s, not printed "
                "(over %d distinct lines repeating)", self._overflow,
                int(now - self._overflow_since), self._max_lines)
            self._overflow = 0

    def _report(self, r: _Repeat, now: float) -> None:
        self._log.log(self._level(r.text),
                      "mag-usb: %d more times in the last %d s, still "
                      "repeating: %s", r.pending, int(now - r.last_report),
                      r.text)
        r.pending = 0
        r.last_report = now

    def flush(self) -> None:
        """Print the counts not yet printed: mag-usb has exited."""
        now = self._clock()
        for r in self._seen.values():
            if r.pending:
                self._report(r, now)
        if self._overflow:
            self._log.warning("mag-usb: %d more lines, not printed",
                              self._overflow)
            self._overflow = 0


def drain(fd: int, relay: StderrRelay, poll_s: float = 5.0) -> None:
    """Read ``fd`` to EOF, one line at a time into ``relay``, and tick
    the relay at least every ``poll_s`` so a count or a clear line
    prints while mag-usb stays quiet.  Never stops reading: an undrained
    pipe fills and blocks mag-usb's next write."""
    buf = b""
    try:
        while True:
            ready, _, _ = select.select([fd], [], [], poll_s)
            if ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                if len(buf) > _MAX_LINE:
                    lines.append(buf)
                    buf = b""
                for raw in lines:
                    text = raw.decode("utf-8", "replace").strip()
                    if text:
                        relay.line(text)
            relay.tick()
        text = buf.decode("utf-8", "replace").strip()
        if text:
            relay.line(text)
    finally:
        relay.flush()
        os.close(fd)
