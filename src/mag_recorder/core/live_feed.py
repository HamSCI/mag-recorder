"""The `mag` live feed: each spooled sample, offered to live-publisher.

live-publisher (HamSCI/live-publisher) holds the station's one public link.
A client offers it rows as datagrams on a local socket, and live-publisher
forwards only the feeds the operator has turned on.  The feed carries the
sample time and x, y, z, rt.  It never carries a position: mag-usb sends
its location in a separate metadata message, which never reaches here.

Best effort by construction: with no socket, or a full one, the row drops
and recording goes on.  A lost datagram loses one row, never the link.

Frame (little-endian), as live-publisher's frames.py defines it:

    magic b"LS", version 1, kind 2 (series), t (double, UTC seconds),
    f0 0.0 (double), df 1.0, db0 0.0, step 1.0 (floats), name length (B),
    payload length (H), the name, then the values as float32.
"""

from __future__ import annotations

import logging
import math
import socket
import struct
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

SOCKET_PATH = "/run/live-publisher/feeds.sock"
FEED = b"mag"
SERIES = 2
HEAD = struct.Struct("<2sBBddfffBH")
FIELDS = ("x", "y", "z", "rt")


def encode(t: float, values) -> bytes:
    payload = struct.pack("<%df" % len(values), *values)
    return HEAD.pack(b"LS", 1, SERIES, t, 0.0, 1.0, 0.0, 1.0,
                     len(FEED), len(payload)) + FEED + payload


def sample_frame(sample: dict) -> Optional[bytes]:
    """A restamped sample -> one frame, or None when a field is missing or not finite."""
    try:
        t = datetime.fromisoformat(sample["ts"].replace("Z", "+00:00")).timestamp()
        values = [float(sample[k]) for k in FIELDS]
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if not all(math.isfinite(v) for v in values):
        return None
    return encode(t, values)


class LiveFeed:
    def __init__(self, path: str = SOCKET_PATH):
        self.path = path
        self.sent = 0
        self.dropped = 0
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self._was_up: Optional[bool] = None

    def offer(self, sample: dict) -> None:
        frame = sample_frame(sample)
        if frame is None:
            self.dropped += 1
            return
        try:
            self._sock.sendto(frame, self.path)
        except OSError:
            self.dropped += 1
            self._note(False)
            return
        self.sent += 1
        self._note(True)

    def _note(self, up: bool) -> None:
        if up != self._was_up:
            if up:
                logger.info("live feed: offering mag rows to %s", self.path)
            elif self._was_up:
                logger.info("live feed: %s stopped taking rows", self.path)
            self._was_up = up
